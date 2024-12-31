# Copyright 2020 - 2022 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
from functools import partial
import nibabel as nib
import numpy as np
import torch
import torch.nn.functional as F
from torch.cuda.amp import GradScaler, autocast
import SimpleITK as sitk
from monai.inferers import sliding_window_inference
# from monai.data import decollate_batch
from monai.losses import DiceCELoss
from monai.metrics import DiceMetric
from monai.networks.nets import SwinUNETR
from monai.transforms import *
from monai.utils.enums import MetricReduction
from monai.handlers import StatsHandler, from_engine
import matplotlib.pyplot as plt
from utils.utils import *
from utils.utils import AverageMeter
from PIL import Image
from monai import data, transforms
from monai.data import *
import resource

rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (8192, rlimit[1]))
print('Setting resource limit:', str(resource.getrlimit(resource.RLIMIT_NOFILE)))

os.environ['CUDA_VISIBLE_DEVICES'] = "3"
os.environ['MASTER_ADDR'] = 'localhost'
os.environ['MASTER_PORT'] = '28890'

parser = argparse.ArgumentParser(description="Segmentation pipeline")
parser.add_argument(
    "--test_data_path", default="/data/linshan/FreeTumor/PANORAMA/imagesTr/", type=str, help="test_data_path")
parser.add_argument(
    "--test_label_path", default="/data/linshan/FreeTumor/PANORAMA/labelsTr/", type=str, help="test_data_path")
parser.add_argument(
    "--save_prediction_path", default="./pred/MSD07_PANORAMA_55.87/", type=str, help="test_prediction_path")
parser.add_argument(
    "--trained_pth", default="./runs/logs_free_panc/model_55.87.pt", type=str, help="trained checkpoint directory")

roi = 96
parser.add_argument("--use_normal_dataset", default=True, help="use monai Dataset class")
parser.add_argument("--feature_size", default=48, type=int, help="feature size")
parser.add_argument("--batch_size", default=1, type=int, help="number of batch size")
parser.add_argument("--sw_batch_size", default=1, type=int, help="number of sliding window batch size")
parser.add_argument("--infer_overlap", default=0.75, type=float, help="sliding window inference overlap")
parser.add_argument("--in_channels", default=1, type=int, help="number of input channels")
parser.add_argument("--out_channels", default=3, type=int, help="number of output channels")
parser.add_argument("--a_min", default=-175.0, type=float, help="a_min in ScaleIntensityRanged")
parser.add_argument("--a_max", default=250.0, type=float, help="a_max in ScaleIntensityRanged")
parser.add_argument("--b_min", default=0.0, type=float, help="b_min in ScaleIntensityRanged")
parser.add_argument("--b_max", default=1.0, type=float, help="b_max in ScaleIntensityRanged")
parser.add_argument("--space_x", default=1.5, type=float, help="spacing in x direction")
parser.add_argument("--space_y", default=1.5, type=float, help="spacing in y direction")
parser.add_argument("--space_z", default=1.5, type=float, help="spacing in z direction")
parser.add_argument("--roi_x", default=roi, type=int, help="roi size in x direction")
parser.add_argument("--roi_y", default=roi, type=int, help="roi size in y direction")
parser.add_argument("--roi_z", default=roi, type=int, help="roi size in z direction")
parser.add_argument("--dropout_rate", default=0.0, type=float, help="dropout rate")
parser.add_argument("--distributed", action="store_true", help="start distributed training")
parser.add_argument("--workers", default=16, type=int, help="number of workers")
parser.add_argument("--spatial_dims", default=3, type=int, help="spatial dimension of input data")
parser.add_argument("--use_checkpoint", default=True, help="use gradient checkpointing to save memory")


def get_test_loader(args):
    """
    Creates training transforms, constructs a dataset, and returns a dataloader.

    Args:
        args: Command line arguments containing dataset paths and hyperparameters.
    """
    test_transforms = transforms.Compose([
        LoadImaged(keys=["image", "label"]),
        EnsureChannelFirstd(keys=["image", "label"]),
        Orientationd(keys=["image", "label"], axcodes="RAS"),
        Spacingd(keys=["image", "label"], pixdim=(args.space_x, args.space_y, args.space_z),
                 mode=("bilinear", "nearest")),
        ScaleIntensityRanged(
            keys=["image"],
            a_min=args.a_min,
            a_max=args.a_max,
            b_min=0.0,
            b_max=1.0,
            clip=True,
        ),
        CropForegroundd(keys=["image", "label"], source_key="image"),
        SpatialPadd(keys=["image", "label"], spatial_size=(args.roi_x, args.roi_y, args.roi_z),
                    mode='constant'),
    ])

    # constructing training dataset
    test_img = []
    test_label = []
    test_name = []

    dataset_list = os.listdir(args.test_data_path)

    for item in dataset_list:
        name = item
        print(name)
        test_img_path = os.path.join(args.test_data_path, name)
        test_label_path = os.path.join(args.test_label_path, name[:-12] + '.nii.gz')  # [:-12] +'.nii.gz'

        test_img.append(test_img_path)
        test_label.append(test_label_path)
        test_name.append(name)

    data_dicts_test = [{'image': image, "label": label, 'name': name}
                        for image, label, name in zip(test_img, test_label, test_name)]

    print('test len {}'.format(len(data_dicts_test)))

    test_ds = Dataset(data=data_dicts_test, transform=test_transforms)
    test_loader = DataLoader(
        test_ds, batch_size=1, shuffle=False, num_workers=args.workers, sampler=None, pin_memory=True
    )
    return test_loader, test_transforms


def main():
    args = parser.parse_args()

    test_loader, test_transforms = get_test_loader(args)

    model = SwinUNETR(
        img_size=(args.roi_x, args.roi_y, args.roi_z),
        in_channels=args.in_channels,
        out_channels=args.out_channels,
        feature_size=args.feature_size,
        drop_rate=0.0,
        attn_drop_rate=0.0,
        dropout_path_rate=0.0,
        use_checkpoint=args.use_checkpoint,
        use_v2=True
    )
    inf_size = [args.roi_x, args.roi_y, args.roi_z]
    model_inferer = partial(
        sliding_window_inference,
        roi_size=inf_size,
        sw_batch_size=args.sw_batch_size,
        predictor=model,
        overlap=args.infer_overlap,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_dict = torch.load(args.trained_pth)["state_dict"]
    model.load_state_dict(model_dict, strict=True)
    model.eval()
    model.to(device)

    # enable cuDNN benchmark
    torch.backends.cudnn.benchmark = True

    post_transforms = Compose([EnsureTyped(keys=["pred"]),
                               Invertd(keys=["pred"],
                                       transform=test_transforms,
                                       orig_keys="image",
                                       meta_keys="pred_meta_dict",
                                       orig_meta_keys="image_meta_dict",
                                       meta_key_postfix="meta_dict",
                                       nearest_interp=True,
                                       to_tensor=True),
                               AsDiscreted(keys="pred", argmax=False, to_onehot=None),
                               SaveImaged(keys="pred", meta_keys="pred_meta_dict", output_dir=args.save_prediction_path,
                                          separate_folder=False, folder_layout=None,
                                          resample=False),
                               ])

    acc_func = DiceMetric(include_background=False, reduction=MetricReduction.MEAN, get_not_nans=True)
    run_acc = AverageMeter()
    post_label = AsDiscrete(to_onehot=args.out_channels)
    post_pred = AsDiscrete(argmax=True, to_onehot=args.out_channels)

    cls_num = 3
    num = np.zeros(2)
    all_dice = None

    with torch.no_grad():
        for idx, batch_data in enumerate(test_loader):
            torch.cuda.empty_cache()

            data = batch_data["image"]
            data = data.cuda()

            old_label = batch_data["label"]
            old_label = old_label.cuda()

            label = old_label.clone()
            label[old_label > 0] = 0
            label[old_label == 3] = 1
            label[old_label == 4] = 2

            # print(torch.unique(label))

            name = batch_data['name'][0]

            with autocast(enabled=True):
                logits = model_inferer(data)

            val_labels_list = decollate_batch(label)
            val_labels_convert = [post_label(val_label_tensor) for val_label_tensor in val_labels_list]
            val_outputs_list = decollate_batch(logits)
            val_output_convert = [post_pred(val_pred_tensor) for val_pred_tensor in val_outputs_list]
            acc_func.reset()
            acc_func(y_pred=val_output_convert, y=val_labels_convert)
            acc, not_nans = acc_func.aggregate()

            dice_list_sub = []
            val_outputs = logits.argmax(1)[0].data.cpu().numpy().astype(np.uint8)
            val_labels = label.data.cpu().numpy()[0, 0, :, :, :]
            from utils.utils import dice

            for i in range(1, cls_num):
                num[i - 1] += (np.sum(val_labels == i) > 0).astype(np.uint8)
                organ_Dice = dice(val_outputs == i, val_labels == i)
                dice_list_sub.append(organ_Dice)

            if all_dice is None:
                all_dice = (np.asarray(dice_list_sub)).copy()
            else:
                all_dice = all_dice + np.asarray(dice_list_sub)

            run_acc.update(acc.cpu().numpy(), n=not_nans.cpu().numpy())

            tumor_acc = all_dice / num
            tumor_acc = tumor_acc[-1]
            print(tumor_acc)

            # output
            output = logits.argmax(1)
            batch_data['pred'] = output.unsqueeze(1)
            batch_data = [post_transforms(i) for i in
                          decollate_batch(batch_data)]

            os.rename(os.path.join(args.save_prediction_path, name[:-7]+'_trans.nii.gz'),
                      os.path.join(args.save_prediction_path, name[:-12]+'.nii.gz'))


if __name__ == "__main__":
    main()