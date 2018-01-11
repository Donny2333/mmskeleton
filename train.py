#!/usr/bin/env python
from __future__ import print_function
import argparse
import os
import time
import numpy as np
import yaml
import pickle
# torch
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def import_class(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod


class Processor():
    def __init__(self, arg):
        self.arg = arg
        self.load_data()
        self.load_model()
        self.load_optimizer()
        self.save_arg()

    def load_data(self):
        Feeder = import_class(self.arg.feeder)
        self.data_loader = dict()
        self.data_loader['train'] = torch.utils.data.DataLoader(
            dataset=Feeder(**self.arg.train_feeder_args),
            batch_size=self.arg.batch_size,
            shuffle=True,
            num_workers=self.arg.num_worker)
        self.data_loader['test'] = torch.utils.data.DataLoader(
            dataset=Feeder(**self.arg.test_feeder_args),
            batch_size=self.arg.test_batch_size,
            shuffle=False,
            num_workers=self.arg.num_worker)

    def load_model(self):
        Model = import_class(self.arg.model)
        self.model = Model(**self.arg.model_args).cuda(self.arg.device)
        self.loss = nn.CrossEntropyLoss().cuda(self.arg.device)

        if self.arg.parallel_device:
            if len(self.arg.parallel_device) > 1:
                self.model = nn.DataParallel(
                    self.model,
                    device_ids=self.arg.parallel_device,
                    output_device=self.arg.device)

        if self.arg.weights:
            print('Load weights from {}.'.format(self.arg.weights))
            with open(self.arg.weights, 'r') as f:
                weights = pickle.load(f)

            for w in self.arg.ignore_weights:
                if weights.pop(w, None) is not None:
                    print('Sucessfully Remove Weights: {}.'.format(w))
                else:
                    print('Can Not Remove Weights: {}.'.format(w))

            try:
                self.model.load_state_dict(weights)
            except:
                state = self.model.state_dict()
                diff = list(set(state.keys()).difference(set(weights.keys())))
                print('Can not find these weights:')
                for d in diff:
                    print('  ' + d)
                state.update(weights)
                self.model.load_state_dict(state)

    def load_optimizer(self):
        if self.arg.optimizer == 'SGD':
            self.optimizer = optim.SGD(
                self.model.parameters(),
                lr=self.arg.base_lr,
                momentum=0.9,
                nesterov=self.arg.nesterov,
                weight_decay=self.arg.weight_decay)
            optimor = optim.SGD
        elif self.arg.optimizer == 'Adam':
            self.optimizer = optim.Adam(
                self.model.parameters(),
                lr=self.arg.base_lr,
                weight_decay=self.arg.weight_decay)
        else:
            raise ValueError()

    def save_arg(self):
        # save arg
        arg_dict = vars(self.arg)
        if not os.path.exists(self.arg.work_dir):
            os.makedirs(self.arg.work_dir)
        with open('{}/arg.yaml'.format(self.arg.work_dir), 'w') as f:
            yaml.dump(arg_dict, f)

    def adjust_learning_rate(self, epoch):
        if self.arg.optimizer == 'SGD' or self.arg.optimizer == 'Adam':
            lr = self.arg.base_lr * (
                0.1**np.sum(epoch >= np.array(self.arg.step)))
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = lr
            return lr
        else:
            raise ValueError()

    def print_time(self):
        localtime = time.asctime(time.localtime(time.time()))
        self.print_log("Local current time :  " + localtime)

    def print_log(self, str, print_time=True):
        if print_time:
            localtime = time.asctime(time.localtime(time.time()))
            str = "[ " + localtime + ' ] ' + str
        print(str)
        if self.arg.print_log:
            with open('{}/log.txt'.format(self.arg.work_dir), 'a') as f:
                print(str, file=f)

    def record_time(self):
        self.cur_time = time.time()
        return self.cur_time

    def split_time(self):
        split_time = time.time() - self.cur_time
        self.record_time()
        return split_time

    def train(self, epoch, save_model=False):
        self.model.train()
        self.print_log('Training epoch: {}'.format(epoch + 1))
        loader = self.data_loader['train']
        lr = self.adjust_learning_rate(epoch)
        loss_value = []

        self.record_time()
        timer = dict(dataloader=0.001, model=0.001, statistics=0.001)
        for batch_idx, (data, label) in enumerate(loader):

            # get data
            data = Variable(
                data.float().cuda(self.arg.device), requires_grad=False)
            label = Variable(
                label.long().cuda(self.arg.device), requires_grad=False)
            timer['dataloader'] += self.split_time()

            # forward
            output = self.model(data)
            loss = self.loss(output, label)

            # backward
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            timer['model'] += self.split_time() 

            # statistics           
            loss_value.append(loss.data[0])
            if batch_idx % self.arg.log_interval == 0:
                self.print_log(
                    '\tBatch({}/{}) done. Loss: {:.4f}  lr:{}'.format(
                        batch_idx, len(loader), loss.data[0], lr))
            timer['statistics'] += self.split_time() 

        # statistics of time consumption and loss 
        proportion = { k:'{:02d}%'.format(int(round(v*100/sum(timer.values())))) for k, v in timer.items()}
        self.print_log('\tMean training loss: {}.'.format(np.mean(loss_value)))
        self.print_log('\tTime consumption: [Data]{dataloader}, [Network]{model}'.format(**proportion))

        if save_model:
            model_path = '{}/epoch{}_model.pkl'.format(self.arg.work_dir,
                                                       epoch + 1)
            with open(model_path, 'w') as f:
                pickle.dump(self.model.state_dict(), f)

            self.print_log('The model was saved in {}'.format(model_path))

    def eval(self, epoch, save_score=False, loader_name=['test']):
        self.model.eval()
        self.print_log('Eval epoch: {}'.format(epoch + 1))
        for ln in loader_name:
            loss_value = []
            score_frag = []
            for batch_idx, (data, label) in enumerate(self.data_loader[ln]):
                data = Variable(
                    data.float().cuda(self.arg.device),
                    requires_grad=False,
                    volatile=True)
                label = Variable(
                    label.long().cuda(self.arg.device),
                    requires_grad=False,
                    volatile=True)
                output = self.model(data)
                loss = self.loss(output, label)
                score_frag.append(output.data.cpu().numpy())
                loss_value.append(loss.data[0])
            score = np.concatenate(score_frag)
            score_dict = dict(
                zip(self.data_loader[ln].dataset.sample_name, score))
            self.print_log('\tMean {} loss of {} batches: {}.'.format(
                ln, len(self.data_loader[ln]), np.mean(loss_value)))
            for k in self.arg.show_topk:
                self.print_log('\tTop{}: {:.2f}%'.format(
                    k, 100 * self.data_loader[ln].dataset.top_k(score, k)))

            if save_score:
                with open('{}/epoch{}_{}_score.pkl'.format(
                        self.arg.work_dir, epoch + 1, ln), 'w') as f:
                    pickle.dump(score_dict, f)

    def start(self):
        if self.arg.phase == 'train':
            for epoch in range(self.arg.start_epoch, self.arg.num_epoch):
                save_model = ((epoch + 1) % self.arg.save_interval == 0) or (
                    epoch + 1 == self.arg.num_epoch)
                eval_model = ((epoch + 1) % self.arg.eval_interval == 0) or (
                    epoch + 1 == self.arg.num_epoch)

                self.train(epoch, save_model=save_model)

                if eval_model:
                    self.eval(epoch, save_score=True, loader_name=['test'])
                else:
                    pass
                    # self.print_time()
                    # self.eval(epoch, save_score=False, loader_name=['eval'])

        elif self.arg.phase == 'test':
            epoch = self.arg.start_epoch
            self.eval(epoch, save_score=True, loader_name=['test'])


if __name__ == '__main__':

    # parameter priority: command line > config > default

    parser = argparse.ArgumentParser(
        description='Spatial Temporal Graph Convolution Network')
    parser.add_argument('--work-dir', default=None)
    parser.add_argument('--config', default=None)

    # visulize and debug
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--log-interval', type=int, default=100)
    parser.add_argument('--save-interval', type=int, default=10, metavar='N')
    parser.add_argument('--eval-interval', type=int, default=5, metavar='N')
    parser.add_argument('--print-log', type=str2bool, default=True)
    parser.add_argument('--show-topk', type=int, default=[1, 5], nargs='+')

    # model
    parser.add_argument('--num-class', type=int, default=400)
    parser.add_argument('--model', default=None)
    parser.add_argument('--model-args', type=dict, default=dict())
    parser.add_argument('--weights', default=None)
    parser.add_argument('--ignore-weights', type=str, default=[], nargs='+')

    # feeder
    parser.add_argument('--feeder', default='feeder.feeder')
    parser.add_argument('--num-worker', type=int, default=128)
    parser.add_argument('--train-feeder-args', default=dict())
    parser.add_argument('--test-feeder-args', default=dict())

    # processor
    parser.add_argument('--phase', default='train')

    # optim
    parser.add_argument('--step', type=int, default=[20, 40, 60], nargs='+')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--parallel-device', type=int, default=None, nargs='+')
    parser.add_argument('--optimizer', default='SGD')
    parser.add_argument('--nesterov', type=str2bool, default=False)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--test-batch-size', type=int, default=256)
    parser.add_argument('--start-epoch', type=int, default=0)
    parser.add_argument('--num_epoch', type=int, default=80)
    parser.add_argument('--base_lr', type=float, default=0.01)
    parser.add_argument('--weight-decay', type=float, default=0.0005)

    # load arg form config file
    p = parser.parse_args()
    if p.config is not None:
        with open(p.config, 'r') as f:
            default_arg = yaml.load(f)
        key = vars(p).keys()
        for k in default_arg.keys():
            if k not in key:
                print('WRONG ARG: {}'.format(k))
                assert (k in key)
        parser.set_defaults(**default_arg)

    arg = parser.parse_args()
    print(vars(arg))

    processor = Processor(arg)
    processor.start()