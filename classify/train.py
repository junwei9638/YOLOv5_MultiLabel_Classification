 # YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Train a YOLOv5 classifier model on a classification dataset

Usage - Single-GPU training:
    $ python classify/train.py --model yolov5s-cls.pt --data imagenette160 --epochs 5 --img 224

Usage - Multi-GPU DDP training:
    $ python -m torch.distributed.run --nproc_per_node 4 --master_port 1 classify/train.py --model yolov5s-cls.pt --data imagenet --epochs 5 --img 224 --device 0,1,2,3

Datasets:           --data mnist, fashion-mnist, cifar10, cifar100, imagenette, imagewoof, imagenet, or 'path/to/data'
YOLOv5-cls models:  --model yolov5n-cls.pt, yolov5s-cls.pt, yolov5m-cls.pt, yolov5l-cls.pt, yolov5x-cls.pt
Torchvision models: --model resnet50, efficientnet_b0, etc. See https://pytorch.org/vision/stable/models.html
"""

import argparse
import os
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import yaml
import shutil

import torch
import torch.distributed as dist
import torch.hub as hub
import torch.optim.lr_scheduler as lr_scheduler
import torchvision
from torch.cuda import amp
from tqdm import tqdm
from torchsummary import summary

FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from classify import val as validate
from models.experimental import attempt_load
from models.yolo import ClassificationModel, DetectionModel
from utils.dataloaders import create_classification_dataloader, create_dataloader

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# REVIEW: import check_yaml
from utils.general import (DATASETS_DIR, LOGGER, WorkingDirectory, check_git_status, check_requirements, colorstr,
                           download, increment_path, init_seeds, print_args, yaml_save, check_yaml, check_dataset, check_file, get_latest_run)
from utils.loggers import GenericLogger
from utils.plots import imshow_cls, Plot_What_U_Want
from utils.torch_utils import (ModelEMA, model_info, select_device, smart_DDP,
                               smart_optimizer, smartCrossEntropyLoss, torch_distributed_zero_first, smartCrossEntropy_CSL )

LOCAL_RANK = int(os.getenv('LOCAL_RANK', -1))  # https://pytorch.org/docs/stable/elastic/run.html
RANK = int(os.getenv('RANK', -1))
WORLD_SIZE = int(os.getenv('WORLD_SIZE', 1))


def train(opt, device):
    init_seeds(opt.seed + 1 + RANK, deterministic=True)
    
    # REVIEW: change data = Path(opt.data) to data = opt.data
    save_dir, data, bs, epochs, nw, imgsz, pretrained = \
        opt.save_dir, opt.data, opt.batch_size, opt.epochs, min(os.cpu_count() - 1, opt.workers), \
        opt.imgsz, str(opt.pretrained).lower() == 'true'
    cuda = device.type != 'cpu'

    # REVIEW: add check_dataset
    with torch_distributed_zero_first(LOCAL_RANK):
        data_dict = check_dataset(data)  # check if None
    
    # Directories
    wdir = save_dir / 'weights'
    wdir.mkdir(parents=True, exist_ok=True)  # make dir
    last, best = wdir / 'last.pt', wdir / 'best.pt'

    # Save run settings
    yaml_save(save_dir / 'opt.yaml', vars(opt))

    # Logger
    logger = GenericLogger(opt=opt, console_logger=LOGGER) if RANK in {-1, 0} else None

    # Download Dataset
    '''with torch_distributed_zero_first(LOCAL_RANK), WorkingDirectory(ROOT):
        data_dir = data if data.is_dir() else (DATASETS_DIR / data)
        if not data_dir.is_dir():
            LOGGER.info(f'\nDataset not found ⚠️, missing path {data_dir}, attempting download...')
            t = time.time()
            if str(data) == 'imagenet':
                subprocess.run(f"bash {ROOT / 'data/scripts/get_imagenet.sh'}", shell=True, check=True)
            else:
                url = f'https://github.com/ultralytics/yolov5/releases/download/v1.0/{data}.zip'
                download(url, dir=data_dir.parent)
            s = f"Dataset download success ✅ ({time.time() - t:.1f}s), saved to {colorstr('bold', data_dir)}\n"
            LOGGER.info(s)'''

    # REVIEW: change type path to str
    # Hyperparameters
    if opt.hyp != None:
        opt.hyp = str( opt.hyp )
        with open(opt.hyp, errors='ignore') as f:
            hyp = yaml.safe_load(f)  # load hyps dict
        LOGGER.info(colorstr('hyperparameters: ') + ', '.join(f'{k}={v}' for k, v in hyp.items()))
        opt.hyp = hyp.copy()  # for saving hyps to checkpoints
    
    classes = data_dict['names']
    trainloader = create_classification_dataloader(data=data_dict,
                                                   mode='train',
                                                   imgsz=imgsz,
                                                   batch_size=bs // WORLD_SIZE,
                                                   augment=True,
                                                   cache=opt.cache,
                                                   rank=LOCAL_RANK,
                                                   workers=nw,
                                                   hyp=opt.hyp)

    valloader = create_classification_dataloader(data=data_dict,
                                                   mode='val',
                                                   imgsz=imgsz,
                                                   batch_size=bs // WORLD_SIZE,
                                                   augment=True,
                                                   cache=opt.cache,
                                                   rank=LOCAL_RANK,
                                                   workers=nw)
    
    testloader = create_classification_dataloader(data=data_dict,
                                                   mode='val',
                                                   imgsz=imgsz,
                                                   batch_size=bs // WORLD_SIZE,
                                                   augment=True,
                                                   cache=opt.cache,
                                                   rank=LOCAL_RANK,
                                                   workers=nw)
    nc = int( data_dict["nc"] )


    # REVIEW: add testloader and test_dir turn into val_dir 
    # Dataloaders
    '''nc = len([x for x in (data_dir / 'train').glob('*') if x.is_dir()])  # number of classes
    trainloader = create_classification_dataloader(path=data_dir / 'train',
                                                   imgsz=imgsz,
                                                   batch_size=bs // WORLD_SIZE,
                                                   augment=True,
                                                   cache=opt.cache,
                                                   rank=LOCAL_RANK,
                                                   workers=nw)
    
    
    # test_dir = data_dir / 'test' if (data_dir / 'test').exists() else data_dir / 'val'  # data/test or data/val
    val_dir = data_dir / 'val' #  data/val
    if RANK in {-1, 0}:
        valloader = create_classification_dataloader(path=val_dir,
                                                      imgsz=imgsz,
                                                      batch_size=bs // WORLD_SIZE * 2,
                                                      augment=False,
                                                      cache=opt.cache,
                                                      rank=-1,
                                                      workers=nw)

    test_dir = data_dir / 'test' #  data/val
    if RANK in {-1, 0}:
        testloader = create_classification_dataloader(path=test_dir,
                                                      imgsz=imgsz,
                                                      batch_size=bs // WORLD_SIZE * 2,
                                                      augment=False,
                                                      cache=opt.cache,
                                                      rank=-1,
                                                      workers=nw)'''
    # REVIEW: add opt.cfg to import model from yaml
    # Model
    with torch_distributed_zero_first(LOCAL_RANK), WorkingDirectory(ROOT):
        if opt.model is not None and ( Path(opt.model).is_file() or opt.model.endswith('.pt') ):
            model = attempt_load(opt.model, device='cpu', fuse=False)

        elif opt.model in torchvision.models.__dict__:  # TorchVision models i.e. resnet50, efficientnet_b0
            model = torchvision.models.__dict__[opt.model](weights='IMAGENET1K_V1' if pretrained else None)

        # TODO: fix bugs
        elif opt.resume:
            weights, epochs, hyp, batch_size = opt.weights, opt.epochs, opt.hyp, opt.batch_size
            model = torch.load( weights )
        
        # Resume: change the way of loading model
        elif opt.cfg is not None : 
            LOGGER.info( "Loading Model from yaml................" ) 
            check_yaml(opt.cfg)
            model = ClassificationModel(model=opt.model, cfg=opt.cfg, nc=nc, cutoff=opt.cutoff or 10)
        else:
            m = hub.list('ultralytics/yolov5')  # + hub.list('pytorch/vision')  # models
            raise ModuleNotFoundError(f'--model {opt.model} not found. Available models are: \n' + '\n'.join(m))

        '''if isinstance(model, DetectionModel):
            LOGGER.warning("WARNING ⚠️ pass YOLOv5 classifier model with '-cls' suffix, i.e. '--model yolov5s-cls.pt'")
            model = ClassificationModel(model=model, nc=nc, cutoff=opt.cutoff or 10)  # convert to classification model'''

        # reshape_classifier_output(model, nc)  # update class count

    # REVIEW: add freeze 
    freeze = [f'model.{x}.' for x in (opt.freeze if len(opt.freeze) > 1 else range(opt.freeze[0]))]  # layers to freeze
    for k, v in model.named_parameters():
        v.requires_grad = True  # train all layers
        if any(x in k for x in freeze):
            LOGGER.info(f'freezing {k}')
            v.requires_grad = False
            
    for m in model.modules():
        if not pretrained and hasattr(m, 'reset_parameters'):
            LOGGER.info( ' -----------reset_parameters----------- ')
            m.reset_parameters()
        if isinstance(m, torch.nn.Dropout) and opt.dropout is not None:
            m.p = opt.dropout  # set dropout
    
    # REVIEW: freeze already do this
    # for p in model.parameters():
    #     p.requires_grad = True  # for training
    
    model = model.to(device)

    #REVIEW: open a file for writing the summary
    with open(os.path.join( save_dir, 'model.txt'), 'w') as f:
        f.write( str(model) )
        f.write( 'Freeze layer: '+ str(opt.freeze) )

        
    # Info
    if RANK in {-1, 0}:
        model.names = classes  # attach class names
        model.transforms = valloader.dataset.album_transforms # attach inference transforms
        # model.names = trainloader.dataset.classes  # attach class names
        # model.transforms = valloader.dataset.torch_transforms # attach inference transforms
        
        model_info(model)
        if opt.verbose:
            LOGGER.info(model)
        
        # REVIEW: catch one train batch info with dataloader
        batch_images, batch_labels = next(iter(trainloader))
        file = imshow_cls(batch_images[:25], batch_labels[:25], names=model.names, f=save_dir / 'train_images.jpg')
        
        logger.log_images(file, name='Train Examples')
        logger.log_graph(model, imgsz)  # log model
    
    # Optimizer
    optimizer = smart_optimizer(model, opt.optimizer, opt.lr0, momentum=0.9, decay=opt.decay)
    
    # Scheduler
    lrf = 0.01  # final lr (fraction of lr0)
    # lf = lambda x: ((1 + math.cos(x * math.pi / epochs)) / 2) * (1 - lrf) + lrf  # cosine
    lf = lambda x: (1 - x / epochs) * (1 - lrf) + lrf  # linear
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)
    # scheduler = lr_scheduler.OneCycleLR(optimizer, max_lr=lr0, total_steps=epochs, pct_start=0.1,
    #                                    final_div_factor=1 / 25 / lrf)

    # EMA
    ema = ModelEMA(model) if RANK in {-1, 0} else None

    # REVIEW: add parallel gpus to training
    if cuda and RANK == -1 and torch.cuda.device_count() > 1:
        LOGGER.warning('WARNING ⚠️ DP not recommended, use torch.distributed.run for best DDP Multi-GPU results.\n'
                       'See Multi-GPU Tutorial at https://github.com/ultralytics/yolov5/issues/475 to get started.')
        model = torch.nn.DataParallel(model)

    # DDP mode
    if cuda and RANK != -1:
        model = smart_DDP(model)

    # Train
    t0 = time.time()
    
    # REVIEW: change to Focal loss
    if opt.csl == 0:
        LOGGER.info( f"{colorstr('Loss Function: ')} smartCrossEntropy" )
        criterion = smartCrossEntropyLoss( label_smoothing=opt.label_smoothing )  # loss function
    else:
        LOGGER.info( f"{colorstr('Loss Function: ')} CSL with CrossEntropy" )
        criterion = smartCrossEntropy_CSL( sigma=opt.csl,  save_dir=save_dir, device=device )
    # criterion = hub.load( 'adeelh/pytorch-multi-class-focal-loss', model='focal_loss', alpha=None, gamma=2, device=device, reduction='mean', force_reload=False )

    best_fitness = 0.0
    scaler = amp.GradScaler(enabled=cuda)
    
    # REVIEW: make val directly
    val = 'val'
    #val = val_dir.stem  # 'val' or 'test'

    
    LOGGER.info(f'Image sizes {imgsz} train, {imgsz} test\n'
                f'Using {nw * WORLD_SIZE} dataloader workers\n'
                f"Logging results to {colorstr('bold', save_dir)}\n"
                f'Starting training on {data} dataset with {nc} classes for {epochs} epochs...\n\n'
                f"{'Epoch':>10}{'GPU_mem':>10}{'train_loss':>12}{f'{val}_loss':>12}{'top1_acc':>12}{'top5_acc':>12}")
    for epoch in range(epochs):  # loop over the dataset multiple times

        tloss, vloss, fitness = 0.0, 0.0, 0.0  # train loss, val loss, fitness
        tloss24, tloss37, tloss51 = 0.0, 0.0, 0.0
        model.train()
        if RANK != -1:
            trainloader.sampler.set_epoch(epoch)
        pbar = enumerate(trainloader)
        if RANK in {-1, 0}:
            pbar = tqdm(enumerate(trainloader), total=len(trainloader), bar_format='{l_bar}{bar:10}{r_bar}{bar:-10b}')
        for i, (images, labels) in pbar:  # progress bar
            images, labels = images.to(device, non_blocking=True), labels.to(device)

            # Forward
            with amp.autocast(enabled=cuda):  # stability issues when enabled
                
                preds = model( images )
                
                # REVIEW: 3 layer
                # preds_layer24 = preds[:, :360]
                # preds_layer37 = preds[:, 360:720]
                # preds_layer51 = preds[:, 720:]
                # preds_mean = ( preds_layer24 + preds_layer37 + preds_layer51 ) / 3
                # loss24 = criterion( preds_layer24, labels )
                # loss37 = criterion( preds_layer37, labels )
                # loss51 = criterion( preds_layer51, labels )
                # loss = loss24 + loss37 + loss51
                
                loss = criterion( preds, labels )
                # loss = criterion( preds_mean, labels )
            
            # Backward
            
            # REVIEW: nn.adaptivePool needs this
            # torch.use_deterministic_algorithms(False)
            
            # REVIEW: nn.Upsample, [None, 1, 'bicubic'] needs this
            torch.use_deterministic_algorithms(mode=True, warn_only=True)
            
            scaler.scale(loss).backward()

            # Optimize
            scaler.unscale_(optimizer)  # unscale gradients
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=10.0)  # clip gradients
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()
            if ema:
                ema.update(model)

            if RANK in {-1, 0}:
                # Print
                tloss = (tloss * i + loss.item()) / (i + 1)  # update mean losses
                
                # REVIEW: 3 layer
                # tloss24 = (tloss24 * i + loss24.item()) / (i + 1)  # update mean losses
                # tloss37 = (tloss37 * i + loss37.item()) / (i + 1)  # update mean losses
                # tloss51 = (tloss51 * i + loss51.item()) / (i + 1)  # update mean losses
                
                mem = '%.3gG' % (torch.cuda.memory_reserved() / 1E9 if torch.cuda.is_available() else 0)  # (GB)
                pbar.desc = f"{f'{epoch + 1}/{epochs}':>10}{mem:>10}{tloss:>12.3g}" + ' ' * 36

                # Test
                if i == len(pbar) - 1:  # last batch
                    top1, top5, vloss, wrong_preds, targets, topk, gt_loc, correct, bias_topk, bias_list, y_total, ytotal_post, image_list = validate.run(model=ema.ema,
                                                     dataloader=valloader,
                                                     criterion=criterion,
                                                     pbar=pbar,
                                                     nc=nc,
                                                     angle_threshold=opt.thresh,
                                                     save_dir=save_dir, 
                                                     epoch=epoch,
                                                     median=opt.median
                                                     )  # test accuracy, loss
                    
                    # REVIEW: 3 layer
                    fitness = top1  # define fitness as top1 accuracy
                    # fitness = top1[-1]  # define fitness as top1 accuracy


        # Scheduler
        scheduler.step()
        

        # REVIEW: plot distribution after val
        img_list, label_list, pred_list = [], [], []
        
        # for i, (val_batch_images, val_batch_labels) in enumerate( next(iter(valloader)) ) :
        #     val_batch_pred = ema.ema(val_batch_images.to(device))
        #     img_list.append( val_batch_images )
        #     label_list.append( val_batch_labels )
        #     pred_list.append( val_batch_pred  )
        
        # img_list, label_list, pred_list = torch.cat(img_list), torch.cat(label_list), torch.cat(pred_list)
        # REVIEW: 3 layer
        # val_batch_pred = ( val_batch_pred[:, :360] + val_batch_pred[:, 360:720] + val_batch_pred[:, 720:1080] ) / 3
        # Plot_What_U_Want( func_name='prob_dis', save_dir=save_dir, epoch=epoch, preds=val_batch_pred, targets=val_batch_labels)
        # Plot_What_U_Want( func_name='wrong_dis', save_dir=save_dir, epoch=epoch, preds=wrong_preds)
        # Plot_What_U_Want( func_name='topk_dis', save_dir=save_dir, epoch=epoch, preds=topk, targets=targets)
        # Plot_What_U_Want( func_name='ang_bias_dis', save_dir=save_dir, epoch=epoch, preds=bias_preds)
        # Plot_What_U_Want( func_name='gt_loc', save_dir=save_dir, epoch=epoch, preds=gt_loc)
        # Plot_What_U_Want( func_name='topk_cdf', save_dir=save_dir, epoch=epoch, preds=correct )
        # Plot_What_U_Want( func_name='bias_topk', save_dir=save_dir, epoch=epoch, preds=bias )
        # Plot_What_U_Want( func_name='bias_mid_top1', save_dir=save_dir, epoch=epoch, preds=bias_list )
        # Plot_What_U_Want( func_name='prob_dis_bias', save_dir=save_dir, epoch=epoch, preds=[y_total, ytotal_post, image_list], targets=targets)
        # Plot_What_U_Want( func_name='value_difference', save_dir=save_dir, epoch=epoch, preds=wrong_values )
        # Plot_What_U_Want( func_name='gaussian', save_dir=save_dir, epoch=epoch, preds=preds, targets=preds_before_gaussian)
        # Log metrics
        if RANK in {-1, 0}:
            # Best fitness
            if fitness > best_fitness:
                best_fitness = fitness

            # Log
            # REVIEW: top15
            metrics = {
                #REVIEW: 3 layer
                "train/loss": tloss,
                f"{val}/loss": vloss,
                "metrics/accuracy_top1": top1,
                "metrics/accuracy_top15": top5,
                # "train/loss24": tloss24,
                # "train/loss37": tloss37,
                # "train/loss51": tloss51,
                # f"{val}/loss24": vloss[1],
                # f"{val}/loss37": vloss[2],
                # f"{val}/loss51": vloss[3],
                # "metrics_24/accuracy_top1": top1[0],
                # "metrics_24/accuracy_top15": top5[0],
                # "metrics_37/accuracy_top1": top1[1],
                # "metrics_37/accuracy_top15": top5[1],
                # "metrics_51/accuracy_top1": top1[2],
                # "metrics_51/accuracy_top15": top5[2],
                "lr/0": optimizer.param_groups[0]['lr']}  # learning rate
            
            logger.log_metrics(metrics, epoch)

            # Save model
            final_epoch = epoch + 1 == epochs
            if (not opt.nosave) or final_epoch:
                ckpt = {
                    'epoch': epoch,
                    'best_fitness': best_fitness,
                    'model': deepcopy(ema.ema).half(),  # deepcopy(de_parallel(model)).half(),
                    'ema': None,  # deepcopy(ema.ema).half(),
                    'updates': ema.updates,
                    'optimizer': None,  # optimizer.state_dict(),
                    'opt': vars(opt),
                    'date': datetime.now().isoformat()}
                # Save last, best and delete
                torch.save(ckpt, last)

                if best_fitness == fitness:
                    torch.save(ckpt, best)

                    # # REVIEW: write best result while validation
                    # val_pred = ema.ema(val_batch_images.to(device))
                    # # REVIEW: 3 layer
                    # # val_pred = torch.max( val_pred[:, 720:] , 1)[1]
                    # val_pred = torch.max( val_pred , 1)[1]
                    # file = imshow_cls(val_batch_images[:25], val_batch_labels[:25], pred = val_pred[:25], test_cls=valloader.dataset.classes, names=trainloader.dataset.classes, f=save_dir / 'best_val_images.jpg')
                    # WriteReport( val_batch_labels, val_pred, save_dir, valloader.dataset.classes, 'best_val' )
                del ckpt

    # Train complete
    if RANK in {-1, 0} and final_epoch:
        LOGGER.info(f'\nTraining complete ({(time.time() - t0) / 3600:.3f} hours)'
                    f"\nResults saved to {colorstr('bold', save_dir)}"
                    f"\nPredict:         python classify/predict.py --weights {best} --source im.jpg"
                    f"\nValidate:        python classify/val.py --weights {best} --data {data_dict['val']}"
                    f"\nExport:          python export.py --weights {best} --include onnx"
                    f"\nPyTorch Hub:     model = torch.hub.load('ultralytics/yolov5', 'custom', '{best}')"
                    f"\nVisualize:       https://netron.app\n")

        # # Plot final epoch examples
        # # REVIEW: add cls_names to solve the problem that nn.DataParallel has no attribute of name
        # val_batch_images, val_batch_labels = (x[:25] for x in next(iter(valloader)))  # first 25 images and labels
        # val_pred = ema.ema(val_batch_images.to(device))
        # # REVIEW: 3 layer
        # # val_pred = torch.max( val_pred[:, 720:] , 1)[1]
        # val_pred = torch.max( val_pred , 1)[1]
        # file = imshow_cls(val_batch_images[:25], val_batch_labels[:25], pred = val_pred[:25], test_cls=valloader.dataset.classes, names=trainloader.dataset.classes, f=save_dir / 'last_val_images.jpg')

        # Log results
        meta = {"epochs": epochs, "top1_acc": best_fitness, "date": datetime.now().isoformat()}
        logger.log_images(file, name='Test Examples (true-predicted)', epoch=epoch)
        logger.log_model(best, epochs, metadata=meta)

    # # REVIEW: Test best model
    # best_model = torch.hub.load( '.', 'custom', path=best, source='local' )
    # test_batch_images, test_batch_labels = next(iter(testloader))
    # test_pred = best_model(test_batch_images.to(device))
    # # REVIEW: 3 layer
    # # test_pred = torch.max( test_pred[:, 720:] , 1)[1]
    # test_pred = torch.max( test_pred , 1)[1]
    # file = imshow_cls(test_batch_images[:25], test_batch_labels[:25], pred = test_pred[:25], test_cls=testloader.dataset.classes, names=trainloader.dataset.classes, f=save_dir / 'test_images.jpg')
    
    # # REVIEW: Write Report
    # WriteReport( test_batch_labels, test_pred, save_dir, testloader.dataset.classes, 'test' )



# REVIEW: add opt-cfg to open yaml
def parse_opt(known=False):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default=None, help='initial weights path')
    parser.add_argument('--cfg', type=str, default=None, help='model yaml file')
    parser.add_argument('--data', type=str, default='imagenette160', help='cifar10, cifar100, mnist, imagenet, ...')
    parser.add_argument('--epochs', type=int, default=10, help='total training epochs')
    parser.add_argument('--batch-size', type=int, default=64, help='total batch size for all GPUs')
    parser.add_argument('--imgsz', '--img', '--img-size', type=int, default=224, help='train, val image size (pixels)')
    parser.add_argument('--nosave', action='store_true', help='only save final checkpoint')
    parser.add_argument('--cache', type=str, nargs='?', const='ram', help='--cache images in "ram" (default) or "disk"')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--workers', type=int, default=2, help='max dataloader workers (per RANK in DDP mode)')
    parser.add_argument('--project', default=ROOT / 'runs/train-cls', help='save to project/name')
    parser.add_argument('--name', default='exp', help='save to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--pretrained', nargs='?', const=True, default=True, help='start from i.e. --pretrained False')
    parser.add_argument('--optimizer', choices=['SGD', 'Adam', 'AdamW', 'RMSProp'], default='Adam', help='optimizer')
    parser.add_argument('--lr0', type=float, default=0.001, help='initial learning rate')
    parser.add_argument('--decay', type=float, default=5e-5, help='weight decay')
    parser.add_argument('--label-smoothing', type=float, default=0.1, help='Label smoothing epsilon')
    parser.add_argument('--cutoff', type=int, default=None, help='Model layer cutoff index for Classify() head')
    parser.add_argument('--dropout', type=float, default=None, help='Dropout (fraction)')
    parser.add_argument('--verbose', action='store_true', help='Verbose mode')
    parser.add_argument('--seed', type=int, default=0, help='Global training seed')
    parser.add_argument('--local_rank', type=int, default=-1, help='Automatic DDP Multi-GPU argument, do not modify')
    parser.add_argument('--hyp', type=str, default=None, help='hyperparameters path')
    parser.add_argument('--rect', action='store_true', help='rectangular training')
    parser.add_argument('--image-weights', action='store_true', help='use weighted image selection for training')
    parser.add_argument('--quad', action='store_true', help='quad dataloader')
    parser.add_argument('--resume', nargs='?', const=True, default=False, help='resume most recent training')
    parser.add_argument('--overwrite', action='store_true', default=False,help='overwrite the project')
    parser.add_argument('--thresh', type=int, default=0, help='angle threshold')
    parser.add_argument('--csl', type=int, default=0, help='csl sigma')
    parser.add_argument('--freeze', nargs='+', type=int, default=[0], help='Freeze layers: backbone=10, first3=0 1 2')
    parser.add_argument('--median', action='store_true', help='median filter')
    return parser.parse_known_args()[0] if known else parser.parse_args()


def main(opt):
    # Checks
    if RANK in {-1, 0}:
        print_args(vars(opt))
        check_git_status()
        check_requirements()
        
    # REVIEW: add overwrite argument 
    if opt.overwrite:
        overwrite_path = os.path.join( os.getcwd(),'runs','train-cls', opt.name )
        if os.path.exists( overwrite_path ) :
            LOGGER.info( f"{colorstr('Overwrite Path: ')}{opt.name}" )

            shutil.rmtree( overwrite_path )
        else : 
            LOGGER.info( f"{colorstr('NO DIRECTORY TO OVERWRITE !!')}" )

    # TODO: add Resume to classify 
    # Resume (from specified or most recent last.pt)
    if opt.resume :
        last = Path(check_file(opt.resume) if isinstance(opt.resume, str) else get_latest_run())
        opt_yaml = last.parent.parent / 'opt.yaml'  # train options yaml
        opt_data = opt.data  # original dataset
        if opt_yaml.is_file():
            with open(opt_yaml, errors='ignore') as f:
                d = yaml.safe_load(f)
        else:
            d = torch.load(last, map_location='cpu')['opt']
        opt = argparse.Namespace(**d)  # replace
        opt.cfg, opt.weights, opt.resume = '', str(last), True  # reinstate
        '''if is_url(opt_data):
            opt.data = check_file(opt_data)  # avoid HUB resume auth timeout'''


    # DDP mode
    device = select_device(opt.device, batch_size=opt.batch_size)
    if LOCAL_RANK != -1:
        assert opt.batch_size != -1, 'AutoBatch is coming soon for classification, please pass a valid --batch-size'
        assert opt.batch_size % WORLD_SIZE == 0, f'--batch-size {opt.batch_size} must be multiple of WORLD_SIZE'
        assert torch.cuda.device_count() > LOCAL_RANK, 'insufficient CUDA devices for DDP command'
        torch.cuda.set_device(LOCAL_RANK)
        device = torch.device('cuda', LOCAL_RANK)
        dist.init_process_group(backend="nccl" if dist.is_nccl_available() else "gloo")

    # Parameters
    opt.save_dir = increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok)  # increment run

    # Train
    train(opt, device)


def run(**kwargs):
    # Usage: from yolov5 import classify; classify.train.run(data=mnist, imgsz=320, model='yolov5m')
    opt = parse_opt(True)
    for k, v in kwargs.items():
        setattr(opt, k, v)
    main(opt)
    return opt


if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
