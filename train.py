import os
import time
import logging
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
from progress.bar import IncrementalBar
from copy import deepcopy
import numpy as np

from models.models import get_model

from data import load_data
from parameter_server import ParameterServer
from statistics import Statistics


def main(args):
    time_stamp = '{:.0f}'.format(time.time() % 100000)
    if torch.cuda.is_available() is True:
        logging.info('Utilizing GPU', extra=args.client)
        print('Utilizing GPU')

    train_loader, val_loader = load_data(args)
    model = get_model(args.model, args)

    if args.batch_size > 256 and args.dataset == 'imagenet' and args.model == 'resnet':
        batch_accumulate_num = args.batch_size // 256
    else:
        batch_accumulate_num = 1
    # create model
    if args.dataset == 'imagenet':
        if batch_accumulate_num > 1:
            args.iterations_per_epoch = len(train_loader.dataset.imgs) // 256
        else:
            args.iterations_per_epoch = len(train_loader.dataset.imgs) // args.batch_size
        val_len = len(val_loader.dataset.imgs) // 1024
    else:
        args.iterations_per_epoch = len(train_loader.dataset.train_labels) // args.batch_size
        val_len = len(val_loader.dataset.test_labels) // 1024

    # get the number of model parameters
    log_str = 'Number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()]))
    logging.info(log_str, extra=args.client)
    print(log_str)
    # for training on multiple GPUs.
    model = torch.nn.DataParallel(model)
    model = model.cuda()
    server = ParameterServer.get_server(args.optimizer, model, args)
    val_statistics = Statistics.get_statistics('image_classification', args)
    train_statistics = Statistics.get_statistics('image_classification', args)
    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume + '/checkpoint.pth.tar'):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume + '/checkpoint.pth.tar')
            args.start_epoch = checkpoint['epoch']
            server = checkpoint['server']
            val_statistics = checkpoint['val_stats']
            train_statistics = checkpoint['train_stats']
            model.load_state_dict(checkpoint['state_dict'])
            print('=> loaded checkpoint {} (epoch {})'.format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    # # Synchronous to Asynchronous Adjustments
    # print('Resetting Parameter Server to Asynchronous Mode')
    # logging.info('Resetting Parameter Server to Asynchronous Mode', extra=args.client)
    # server._shards_weights = list()
    # weights = server._get_model_weights()
    # for i in range(0, args.workers_num):
    #     server._shards_weights.append(deepcopy(weights))
    # server._workers_num = args.workers_num
    # # learning rate initialization
    # batch_baseline = args.baseline
    # server._lr = args.lr * np.sqrt((args.workers_num * args.batch_size) // batch_baseline) / (args.workers_num)
    # server._fast_im = args.fast_im
    # server._lr_warm_up = args.lr_warm_up
    # server._current_lr = args.lr
    # server._m_off = args.m_off
    # server._current_momentum = args.momentum
    # server._iterations_per_epoch = args.iterations_per_epoch
    # server._momentum = args.momentum
    # server._client = args.client
    # if args.fast_im is True:
    #     end_lr = args.lr * ((args.workers_num * args.batch_size) // batch_baseline) / np.sqrt(args.workers_num)
    #     start_lr = args.lr / (args.workers_num)
    #     server._lr = end_lr
    #     server._start_lr = start_lr
    #     server._lr_increment_const = (end_lr - start_lr) / (args.iterations_per_epoch * 5)
    #     log_str = 'Fast ImageNet Mode - Warm Up [{:.5f}]->[{:.5f}] In 5 Epochs'.format(start_lr, end_lr)
    #     logging.info(log_str, extra=args.client)
    #     print(log_str)
    # else:
    #     server._start_lr = 0
    #     server._lr_increment_const = 0
    # for param_group in server._optimizer.param_groups:
    #     param_group['lr'] = start_lr
    #     param_group['momentum'] = server._momentum
    # # Synchronous to Asynchronous Adjustments - End

    cudnn.benchmark = True
    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()
    if args.bar is True:
        train_bar = IncrementalBar('Training  ', max=args.iterations_per_epoch, suffix='%(percent)d%%')
        val_bar = IncrementalBar('Evaluating', max=val_len, suffix='%(percent)d%%')
    else:
        train_bar = None
        val_bar = None
    log_str = '{}: Training neural network for {} epochs with {} workers'.format(args.id, args.epochs,
                                                                                 args.workers_num)
    logging.info(log_str, extra=args.client)
    print(log_str)
    train_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        # train for one epoch
        train_loss, train_error = train(train_loader, model, criterion, server, epoch, args.workers_num, args.grad_clip,
                                        batch_accumulate_num, train_bar, train_statistics, args.client)

        train_time = time.time() - train_time
        if args.bar is True:
            train_bar.finish()
            train_bar.index = 0

        # evaluate on validation set
        val_time = time.time()
        with torch.no_grad():
            val_loss, val_error = validate(val_loader, model, criterion, server, val_statistics, val_bar)
        train_statistics.save_loss(train_loss)
        train_statistics.save_error(train_error)
        train_statistics.save_weight_mean_dist(server.get_workers_mean_statistics())
        train_statistics.save_weight_master_dist(server.get_workers_master_statistics())
        train_statistics.save_mean_master_dist(server.get_mean_master_dist())
        train_statistics.save_weight_norm(server.get_server_weights())
        train_statistics.save_gradient_norm(server.get_server_gradients())
        val_time = time.time() - val_time
        if args.bar is True:
            val_bar.finish()
            val_bar.index = 0

        log_str = 'Epoch [{0:1d}]: Train: Time [{1:.2f}], Loss [{2:.3f}], Error[{3:.3f}] | ' \
                  'Test: Time [{4:.2f}], Loss [{5:.3f}], Error[{6:.3f}]'.format(epoch + 1, train_time, train_loss,
                                                                                train_error, val_time, val_loss,
                                                                                val_error)
        logging.info(log_str, extra=args.client)
        print(log_str)
        if epoch % args.save == 0 and epoch > 0:
            save_checkpoint({'epoch': epoch + 1,
                             'state_dict': model.state_dict(),
                             'val_stats': val_statistics,
                             'train_stats': train_statistics,
                             'server': server}, sim_name=(args.name + time_stamp + '_' + str(epoch)))
        train_time = time.time()

    return train_statistics, val_statistics


def wd_pre_step(model, weight_decay=1e-4):
    with torch.no_grad():
        for m in model.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.Linear):
                m.weight.grad.add_(weight_decay * m.weight)

def train(train_loader, model, criterion, server, epoch, workers_number, grad_clip, batch_accumulate_num, bar,
          statistics, client):
    """Train for one epoch on the training set"""
    train_error = AverageMeter()
    train_loss = AverageMeter()
    # switch to train mode
    model.train()

    for i, (input, target) in enumerate(train_loader):
        current_accumulate_num = i % batch_accumulate_num
        current_worker = (i // batch_accumulate_num) % workers_number
        if current_accumulate_num == 0:
            set_model_weights(server.pull(current_worker), model)
        target = target.cuda(async=True)
        input = input.cuda()
        # compute output
        if current_accumulate_num == 0:
            model.zero_grad()
        output = model(input)
        loss = criterion(output, target)
        loss.div_(batch_accumulate_num)  # instead of normalizing gradients
        loss.backward()
        wd_pre_step(model)
        if grad_clip < 1000:
            torch.nn.utils.clip_grad_norm(model.parameters(), grad_clip)
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        train_loss.update(loss.data[0] * batch_accumulate_num, input.size(0))
        train_error.update(100 - prec1[0], input.size(0))
        if current_accumulate_num == (batch_accumulate_num - 1):
            gradients = get_model_gradients(model)
            tau = (i // batch_accumulate_num - current_worker) / workers_number + 1
            step_norm = server.push(current_worker, gradients, epoch, tau=tau, iteration=(i // batch_accumulate_num))
            statistics.save_step_norm(step_norm)
        if bar is not None:
            bar.next()
    return train_loss.avg, train_error.avg


def validate(data_loader, model, criterion, server, statistics, bar):
    """Perform validation on the validation set"""

    server_weights = server.get_server_weights()
    set_model_weights(server_weights, model)
    # switch to evaluate mode
    model.eval()  # TODO: debug evaluation
    error = AverageMeter()
    error_5 = AverageMeter()
    total_loss = AverageMeter()
    for i, (input, target) in enumerate(data_loader):
        target = target.cuda(async=True)
        input = input.cuda()
        # compute output
        output = model(input)
        loss = criterion(output, target)

        # measure accuracy and record loss
        total_loss.update(loss.data[0], input.size(0))
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        error.update(100 - prec1[0], input.size(0))
        error_5.update(100 - prec5[0], input.size(0))
        if bar is not None:
            bar.next()

    statistics.save_loss(total_loss.avg)
    statistics.save_error(error.avg)
    statistics.save_error_top5(error_5.avg)
    return total_loss.avg, error.avg


def set_model_weights(weights, model):
    for name, weight in model.named_parameters():
        # omit 'module.' in name string -> name[7:]
        if torch.cuda.is_available() is True:
            weight.data = weights[name].cuda()
        else:
            weight.data = weights[name]


def get_model_gradients(model):
    gradients = {}
    for name, weight in model.named_parameters():
        gradients[name] = weight.grad.clone()
    return gradients


def get_model_weights(model):
    weights = {}
    for name, weight in model.named_parameters():
        weights[name] = weight.clone()
    return weights


def normalize_gradients(model, norm_factor):
    for weight in model.parameters():
        weight.grad.div_(norm_factor)


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.float().topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def save_checkpoint(state, filename='checkpoint.pth.tar', sim_name=''):
    """Saves checkpoint to disk"""
    name = sim_name
    directory = '/media/drive/ehoffer/backups/%s/' % name
    if not os.path.exists(directory):
        os.makedirs(directory)
    filename = directory + filename
    torch.save(state, filename)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count
