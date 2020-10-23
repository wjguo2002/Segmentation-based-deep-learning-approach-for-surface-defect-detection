'''
train scipts

author: zacario li
date: 2020-10-09
'''

import os
import random
import time
import cv2
import numpy as np
import logging
import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim
import torch.utils.data
import torch.nn.init as initer 
from models import dlasdd
from loss import diceloss
from dataset import data
from utils import transform, common

# global config
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu' 
NUM_CLASSES = 2
DATAROOT = '../datasets/KolektorSDD/'
TRAINLIST = 'train.txt'
VALLIST = 'val.txt'
GLOBALEPOCH = 4000
BASELR = 0.01
INPUTHW = (int(1408/2), int(512/2))
SEGRESUME = 'save/train_sn_3250.pth'

def poly_learning_rate(base_lr, curr_iter, max_iter, power=0.9):
    """poly learning rate policy"""
    lr = base_lr * (1 - float(curr_iter) / max_iter) ** power
    return lr

def weights_init(model):
    for m in model.modules():
        if isinstance(m, (nn.modules.conv._ConvNd)):
            initer.kaiming_normal_(m.weight)
            if m.bias is not None:
                initer.constant_(m.bias, 0)

        elif isinstance(m, (nn.modules.batchnorm._BatchNorm)):
            initer.normal_(m.weight, 1.0, 0.02)
            initer.constant_(m.bias, 0.0)

        elif isinstance(m, nn.Linear):
            initer.kaiming_normal_(m.weight)
            if m.bias is not None:
                initer.constant_(m.bias, 0)

def get_mean_std():
    value_scale = 255
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]
    return mean, std

def prepare_dataset(rootpath, trainlist, vallist, mean, std):
    # train transform template
    trans = transform.Compose([
        transform.Resize(INPUTHW),
        transform.RandomGaussianBlur(),
        transform.RandomHorizontalFlip(),
        transform.ToTensor(),
        transform.Normalize(mean=mean, std=std)
    ])

    # val transform template
    valtrans = transform.Compose([
        transform.Resize(INPUTHW),
        transform.ToTensor(),
        transform.Normalize(mean=mean, std=std)
    ])

    # training data
    train_dataset = data.SemData(split='train', data_root=rootpath, data_list=trainlist, transform=trans)
    train_dataloader = torch.utils.data.DataLoader(train_dataset,
                                                    batch_size=24,
                                                    shuffle=True,
                                                    num_workers=8,
                                                    pin_memory=True,
                                                    drop_last=True)

    # val data
    val_dataset = data.SemData(split='val', data_root=rootpath, data_list=vallist, transform=valtrans)
    val_dataloader = torch.utils.data.DataLoader(val_dataset,
                                                    batch_size=8,
                                                    shuffle=False,
                                                    num_workers=4,
                                                    pin_memory=True)
    
    return train_dataloader, val_dataloader

def sub_sn_train(model, optimizer, criterion, dataloader, currentepoch, maxIter):
    model.train()
    intersectionmeter = common.AverageMeter()
    unionmeter = common.AverageMeter()
    targetmeter = common.AverageMeter()
    lossmeter = common.AverageMeter()

    for i, (x,y) in enumerate(dataloader):
        x = x.to(DEVICE, non_blocking=True)
        y = y.to(DEVICE, non_blocking=True)

        out = model(x)
        out[1] = nn.Upsample(scale_factor=8, mode='bilinear')(out[1])

        loss = criterion(out[1], y)
        lossmeter.update(loss.item(), x.shape[0])

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        curiter = currentepoch * len(dataloader) + i + 1
        newlr = poly_learning_rate(BASELR, curiter, maxIter)
        optimizer.param_groups[0]['lr'] = newlr

        #iou
        result = out[1].max(1)[1]
        intersection, union, target = common.intersectionAndUnionGPU(result, torch.squeeze(y), NUM_CLASSES, 255)
        intersection, union, target = intersection.cpu().numpy(), union.cpu().numpy(), target.cpu().numpy()

        intersectionmeter.update(intersection), unionmeter.update(union), targetmeter.update(target)
    IoU = intersectionmeter.sum/(unionmeter.sum + 1e-10)
    accuracy = intersectionmeter.sum/(targetmeter.sum + 1e-10)
    print(f'[{currentepoch}/{GLOBALEPOCH}] loss:{lossmeter.avg}')

def sub_sn_val(model, criterion, dataloader):
    model.eval()
    intersectionmeter = common.AverageMeter()
    unionmeter = common.AverageMeter()
    targetmeter = common.AverageMeter()
    lossmeter = common.AverageMeter()

    for i, (x,y) in enumerate(dataloader):
        x = x.to(DEVICE, non_blocking=True)
        y = y.to(DEVICE, non_blocking=True)

        out = model(x)
        out[1] = nn.Upsample(scale_factor=8, mode='bilinear')(out[1])

        mainloss = criterion(out[1], y)

        lossmeter.update(mainloss.item(), x.shape[0])
        result = out[1].max(1)[1]
        intersection, union, target = common.intersectionAndUnionGPU(result, torch.squeeze(y), NUM_CLASSES, 255)
        intersection, union, target = intersection.cpu().numpy(), union.cpu().numpy(), target.cpu().numpy()
        intersectionmeter.update(intersection), unionmeter.update(union), targetmeter.update(target)
        # save for debug
        rt = result*128
        rt = rt.to(torch.uint8)
        rt = rt.cpu().numpy()
        cv2.imwrite('segout.jpg',rt[0])
    
    #IoU
    IoU = intersectionmeter.sum/(unionmeter.sum + 1e-10)
    accuracy = intersectionmeter.sum/(targetmeter.sum + 1e-10)
    print(f'val loss:{lossmeter.avg}')
    for i in range(NUM_CLASSES):
        print(f'class_{i} IoU:{IoU[i]}, acc: {accuracy[i]}')
        

def train_seg():
    snmodel = dlasdd.SegNetwork(1,1024,NUM_CLASSES)
    EPOCH_START =0
    if SEGRESUME is None:
        weights_init(snmodel)
    else:
        wt = torch.load(SEGRESUME)
        snmodel.load_state_dict(wt['state_dict'])
        EPOCH_START = wt['epoch']
    if EPOCH_START>= GLOBALEPOCH:
        print(f"current ckpt's epoch[{EPOCH_START}] is smaller and equal than globalepoch[{GLOBALEPOCH}]")
        print(f'stop training...')
        return
    snmodel.to(DEVICE)
    mean, std = get_mean_std()
    train_loader, val_loader = prepare_dataset(DATAROOT, TRAINLIST, VALLIST, mean, std)

    criterion = diceloss.DiceLoss()
    '''
    wt = torch.tensor([0.1, 10000000])
    wt = wt.to(DEVICE)
    criterion = nn.CrossEntropyLoss(weight=wt)
    '''
    optimizer = torch.optim.SGD(snmodel.parameters(), lr=BASELR, momentum=0.9, weight_decay=0.0001)

    maxIter = GLOBALEPOCH * len(train_loader)
    # starting traing
    for epoch in range(EPOCH_START, GLOBALEPOCH):
        sub_sn_train(snmodel, optimizer, criterion, train_loader, epoch, maxIter)
        sub_sn_val(snmodel, criterion, val_loader)
        if (((epoch+1) % 50)) == 0:
            filename = f'save/train_sn_{epoch}.pth'
            torch.save({'epoch':epoch, 'state_dict':snmodel.state_dict(), 'optimizer':optimizer.state_dict()}, filename)

def train_decision():
    pass


if __name__ == '__main__':
    #train seg network
    train_seg()
    #train decision network
    train_decision()
