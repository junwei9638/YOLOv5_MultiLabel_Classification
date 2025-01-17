# YOLOv5 🚀 by Ultralytics, GPL-3.0 license
"""
Validate a trained YOLOv5 classification model on a classification dataset

Usage:
    $ bash data/scripts/get_imagenet.sh --val  # download ImageNet val split (6.3G, 50000 images)
    $ python classify/val.py --weights yolov5m-cls.pt --data ../datasets/imagenet --img 224  # validate ImageNet

Usage - formats:
    $ python classify/val.py --weights yolov5s-cls.pt                 # PyTorch
                                       yolov5s-cls.torchscript        # TorchScript
                                       yolov5s-cls.onnx               # ONNX Runtime or OpenCV DNN with --dnn
                                       yolov5s-cls_openvino_model     # OpenVINO
                                       yolov5s-cls.engine             # TensorRT
                                       yolov5s-cls.mlmodel            # CoreML (macOS-only)
                                       yolov5s-cls_saved_model        # TensorFlow SavedModel
                                       yolov5s-cls.pb                 # TensorFlow GraphDef
                                       yolov5s-cls.tflite             # TensorFlow Lite
                                       yolov5s-cls_edgetpu.tflite     # TensorFlow Edge TPU
                                       yolov5s-cls_paddle_model       # PaddlePaddle
"""

import argparse
import os
import sys
from pathlib import Path
import numpy as np
import torch
from tqdm import tqdm


FILE = Path(__file__).resolve()
ROOT = FILE.parents[1]  # YOLOv5 root directory
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))  # add ROOT to PATH
ROOT = Path(os.path.relpath(ROOT, Path.cwd()))  # relative

from models.common import DetectMultiBackend
from utils.dataloaders import create_classification_dataloader
from utils.general import LOGGER, Profile, check_img_size, check_requirements, colorstr, increment_path, print_args, check_dataset
from utils.torch_utils import select_device, smart_inference_mode, gaussian_filter_1d
from utils.plots import Plot_What_U_Want
from utils.general import MedianFilter, ZScoreFilter, MedianFilter120, MedianFilterForXY, MedianFilterFilterForXYTop5, PredsPostProcess
def CalculateTopk_and_GetWrongSample( pred, pred_post, targets, value, threshold, post_process=False, device=None):
    wrong_preds = []
    bias_ori = []
    gt_loc = []
    topk_list = []
    bias_median = None
    
    if post_process:
        bias_median = MedianFilterFilterForXYTop5( pred, targets, device )

    # REVIEW: get the wrong pred samples and bias_pred
    bias_topk = pred.clone()
    bias_topk_post = pred_post.clone()
    large_bias_count, samll_bias_count, correct_bias_count= 0, 0, 0

    for i, target in enumerate(targets):
        bias_topk[i] = abs( bias_topk[i]-target )
        bias_topk[i] = torch.where( bias_topk[i]<=180, bias_topk[i], 360-bias_topk[i] )
        
        bias_topk_post[i] = abs( bias_topk_post[i]-target )
        bias_topk_post[i] = torch.where( bias_topk_post[i]<=180, bias_topk_post[i], 360-bias_topk_post[i] )
        
        if bias_topk[i, 0] > threshold :
            wrong_preds.append( [pred[i, 0], target] )

        
        if bias_topk[i, 0] >= 170 : 
            large_bias_count += 1 
        elif bias_topk[i, 0] <= 5:
            samll_bias_count += 1
        else:
            correct_bias_count += 1
            
        # if bias_topk_post[i, 0] < 6 and bias_topk[i, 0] > 170:
        #     correct_bias_count += 1
            
            
            # if torch.any( pred[i][:] == target ):
            #     wrong_values.append( [ target, value[i][0], value[i][torch.where( pred[i][:] == target )]] )
        # if pred[i][0] != target :
        #     wrong_preds.append( [pred[i][0].clone(), target])
        
        bias_ori.append( bias_topk[i, 0] )
        
    #REVIEW: get bias of top15 and location of top1
        # if torch.any( bias[i] <= threshold ):
        #     gt_loc.append( torch.where( bias[i] <= threshold )[0][0] )
    print( correct_bias_count, large_bias_count, samll_bias_count )
    # REVIEW: add threshold of angle
    correct = torch.where( bias_topk <= threshold, torch.tensor(1), torch.tensor(0) ).float()
    # correct = (targets[:, None] == pred).float()
    
    acc = torch.stack((correct[:, 0], correct.max(1).values), dim=1)  # (top1, top5) accuracy
    top1, top5 = acc.mean(0).tolist()
    
    for i in range(threshold+1):
        correct = torch.where( bias_topk <= i, torch.tensor(1), torch.tensor(0) ).float()
        acc = torch.stack((correct[:, 0], correct.max(1).values), dim=1)  # (top1, top5) accuracy
        topk_list.append( acc.mean(0).tolist() )
    
    return top1, top5, wrong_preds, gt_loc, correct, bias_topk, [bias_median, bias_ori], topk_list, acc

def Guas_Compare( y, y_gau):
    # print( y[:, 0], y_gau[:, 0])
    diff_count = sum([1 for x, y in zip(y[:, 0], y_gau[:, 0]) if x != y])
    return diff_count

@smart_inference_mode()
def run(
    data=ROOT / '../datasets/mnist',  # dataset dir
    weights=ROOT / 'yolov5s-cls.pt',  # model.pt path(s)
    batch_size=32,  # batch size
    imgsz=224,  # inference size (pixels)
    device='',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
    workers=8,  # max dataloader workers (per RANK in DDP mode)
    verbose=False,  # verbose output
    project=ROOT / 'runs/val-cls',  # save to project/name
    name='exp',  # save to project/name
    exist_ok=False,  # existing project/name ok, do not increment
    half=False,  # use FP16 half-precision inference
    dnn=False,  # use OpenCV DNN for ONNX inference
    model=None,
    dataloader=None,
    criterion=None,
    pbar=None,
    nc=None,
    angle_threshold=0,
    gaussian = None,
    save_dir = None,
    epoch = None,
    median = False
):
    # Initialize/load model and set device

    training = model is not None
    if training:  # called by train.py
        device, pt, jit, engine = next(model.parameters()).device, True, False, False  # get model device, PyTorch model
        half &= device.type != 'cpu'  # half precision only supported on CUDA
        model.half() if half else model.float()
    else:  # called directly
        device = select_device(device, batch_size=batch_size)
        
        # REVIEW: add check_dataset
        data_dict = check_dataset(data)  # check if None
        
        # Directories
        save_dir = increment_path(Path(project) / name, exist_ok=exist_ok)  # increment run
        save_dir.mkdir(parents=True, exist_ok=True)  # make dir

        # Load model
        model = DetectMultiBackend(weights, device=device, dnn=dnn, fp16=half)
        stride, pt, jit, engine = model.stride, model.pt, model.jit, model.engine
        imgsz = check_img_size(imgsz, s=stride)  # check image size
        half = model.fp16  # FP16 supported on limited backends with CUDA
        if engine:
            batch_size = model.batch_size
        else:
            device = model.device
            if not (pt or jit):
                batch_size = 1  # export.py models default to batch-size 1
                LOGGER.info(f'Forcing --batch-size 1 square inference (1,3,{imgsz},{imgsz}) for non-PyTorch models')

        # Dataloader
        # data = Path(data)
        # test_dir = data / 'test' if (data / 'test').exists() else data / 'val'  # data/test or data/val
        dataloader = create_classification_dataloader(data_dict,
                                                      mode='val',
                                                      imgsz=imgsz,
                                                      batch_size=batch_size,
                                                      augment=True,
                                                      rank=-1,
                                                      workers=workers)

    model.eval()
    pred, pred_post, y_total, y_total_post, image_list, value, targets, loss, dt = [], [], [], [], [], [], [], 0, (Profile(), Profile(), Profile())
    loss24, loss37, loss51, gau_count = 0, 0, 0, 0
    n = len(dataloader)  # number of batches
    
    # REVIEW: make action directly
    action = 'validating'
    # action = 'validating' if dataloader.dataset.root.stem == 'val' else 'testing'
    
    desc = f"{pbar.desc[:-36]}{action:>36}" if pbar else f"{action}"
    bar = tqdm(dataloader, desc, n, not training, bar_format='{l_bar}{bar:10}{r_bar}{bar:-10b}', position=0)
    with torch.cuda.amp.autocast(enabled=device.type != 'cpu'):
        for images, labels in bar:
            with dt[0]:
                images, labels = images.to(device, non_blocking=True), labels.to(device)

            with dt[1]:
                
                y = model( images ) 
                # y_before_gau = y.clone()
                # y = gaussian_filter_1d( y, kernel_size=5, sigma=5, save_dir=save_dir, device=device)
                y_postproc = PredsPostProcess( y.clone(), window_size=5 )
                # y = y_postproc
                #REVIEW: gaussian
                # y_before_gau = y.clone()
                # y = gaussian_filter_1d( y, kernel_size=int(gaussian[0]), sigma=int(gaussian[1]), save_dir=save_dir, device=device)
                # gau_count += Guas_Compare(y_before_gau.argsort(1, descending=True)[:, :15], y.argsort(1, descending=True)[:, :15])
                
                #REVIEW: 3 layer
                # y24 = y[:, :360]
                # y37 = y[:, 360:720]
                # y51 = y[:, 720:]
            with dt[2]:
                
                # REVIEW: 3 layer
                # pred24.append(y24.argsort(1, descending=True)[:, :15])
                # pred37.append(y37.argsort(1, descending=True)[:, :15])
                # pred51.append(y51.argsort(1, descending=True)[:, :15])
                # y = ( y24 + y37 + y51 ) / 3 
                y_total.append( y )
                image_list.append( images )
                y_total_post.append( y_postproc )
                pred.append( y.argsort(1, descending=True)[:, :15] )
                pred_post.append( y_postproc.argsort(1, descending=True)[:, :15] )
                value.append( y.sort( 1, descending=True)[0][:, :15] )
                # for i in range(len(labels) ): 
                #     print( '-------------------------' )
                #     print( 'target: ', labels[i] )
                #     print( 'pred: ', y )
                #     print( 'value: ', y.sort( 1, descending=True)[0][:, :15][i] )
                #     print( '-------------------------' )
                targets.append(labels)
                
                if criterion:
                    loss += criterion(y, labels )
                    
                    # REVIEW: 3 layer
                    # loss24 += criterion(y24, labels)
                    # loss37 += criterion(y37, labels)
                    # loss51 += criterion(y51, labels)
                    # loss = loss24 + loss37 + loss51
    
    #REVIEW: gaussian
    # print( 'differet count: ', gau_count )
    # Plot_What_U_Want( func_name='gaussian', save_dir=save_dir, epoch=epoch, preds=y, targets=y_before_gau)
    
    # REVIEW: 3 layers
    # pred24, pred37, pred51, targets = torch.cat(pred24), torch.cat(pred37), torch.cat(pred51), torch.cat(targets)
    # result24 = CalculateTopk_and_GetWrongSample( pred24, targets )
    # result37 = CalculateTopk_and_GetWrongSample( pred37, targets )
    # result51 = CalculateTopk_and_GetWrongSample( pred51, targets )
    # top1 = [result24[0], result37[0], result51[0]]
    # top5 = [result24[1], result37[1], result51[1]]
    # wrong_preds = [result24[2], result37[2], result51[2]]
    pred, pred_post, targets, value, y_total, y_total_post, image_list =  torch.cat(pred), torch.cat(pred_post), torch.cat(targets), torch.cat(value), torch.cat(y_total), torch.cat(y_total_post), torch.cat(image_list)
    top1, top5, wrong_preds, gt_loc, correct, bias_topk, bias_list, topk_list, acc = CalculateTopk_and_GetWrongSample( pred.clone(), pred_post.clone(), targets, value, angle_threshold, post_process=median )
    loss /= n
    
    # REVIEW: 3 layers
    # loss24 /= n
    # loss37 /= n
    # loss51 /= n
    
    
    
    if pbar:
        # REVIEW: 3 layer
        # pbar.desc = f"{pbar.desc[:-36]}{loss:>12.3g}{top1[-1]:>12.3g}{top5[-1]:>12.3g}"
        
        pbar.desc = f"{pbar.desc[:-36]}{loss:>12.3g}{top1:>12.3g}{top5:>12.3g}"
    if verbose:  # all classes
        LOGGER.info(f"{'Class':>24}{'Images':>12}{'top1_acc':>12}{'top5_acc':>12}")
        
        # REVIEW: 3 layer
        # LOGGER.info(f"{'all':>24}{targets.shape[0]:>12}{top1[-1]:>12.3g}{top5[-1]:>12.3g}")
        
        LOGGER.info(f"{'all':>24}{targets.shape[0]:>12}{top1:>12.3g}{top5:>12.3g}")
        for i, c in model.names.items():
            aci = acc[targets == i]
            top1i, top5i = aci.mean(0).tolist()
            LOGGER.info(f"{c:>24}{aci.shape[0]:>12}{top1i:>12.3g}{top5i:>12.3g}")

        # Print results
        t = tuple(x.t / len(dataloader.dataset.samples) * 1E3 for x in dt)  # speeds per image
        shape = (1, 3, imgsz, imgsz)
        LOGGER.info(f'Speed: %.1fms pre-process, %.1fms inference, %.1fms post-process per image at shape {shape}' % t)
        LOGGER.info(f"Results saved to {colorstr('bold', save_dir)}")
    
    #REVIEW: 3 layer
    # return top1, top5, [loss, loss24, loss37, loss51], wrong_preds, targets, [pred24, pred37, pred51]
    for topk in topk_list:
        print( topk[0] )
    # Plot_What_U_Want( func_name='topk_threshold', save_dir=save_dir, epoch=epoch, preds=topk_list )
    # Plot_What_U_Want( func_name='wrong_dis', save_dir=save_dir, epoch=epoch, preds=wrong_preds)
    Plot_What_U_Want( func_name='prob_dis', save_dir=save_dir, epoch=epoch, preds=y_total, targets=targets)
    Plot_What_U_Want( func_name='prob_dis_bias', save_dir=save_dir, epoch=epoch, preds=[y_total, y_total_post, image_list], targets=targets)
    return top1, top5, loss, wrong_preds, targets, pred, gt_loc, correct, bias_topk, bias_list, y_total, y_total_post, image_list


def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, default=ROOT / '../datasets/mnist', help='dataset path')
    parser.add_argument('--weights', nargs='+', type=str, default=ROOT / 'yolov5s-cls.pt', help='model.pt path(s)')
    parser.add_argument('--batch-size', type=int, default=128, help='batch size')
    parser.add_argument('--imgsz', '--img', '--img-size', type=int, default=224, help='inference size (pixels)')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--workers', type=int, default=8, help='max dataloader workers (per RANK in DDP mode)')
    parser.add_argument('--verbose', nargs='?', const=True, default=False, help='verbose output')
    parser.add_argument('--project', default=ROOT / 'runs/val-cls', help='save to project/name')
    parser.add_argument('--name', default='exp', help='save to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--half', action='store_true', help='use FP16 half-precision inference')
    parser.add_argument('--dnn', action='store_true', help='use OpenCV DNN for ONNX inference')
    opt = parser.parse_args()
    print_args(vars(opt))
    return opt


def main(opt):
    check_requirements(exclude=('tensorboard', 'thop'))
    run(**vars(opt))


if __name__ == "__main__":
    opt = parse_opt()
    main(opt)
