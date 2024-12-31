import torch
import torch.nn as nn
from models.Unet import *
from utils.TumorGenerated.utils import *
from utils.TumorGenerated.utils import SynthesisTumor
from functools import partial
from monai.inferers import sliding_window_inference
from monai.losses import DiceCELoss, DiceLoss
from monai.metrics import DiceMetric
from monai.networks.nets import SwinUNETR, UNet
from monai.transforms import Activations, AsDiscrete, Compose
from monai.utils.enums import MetricReduction
from monai.data import decollate_batch


class TGAN(nn.Module):
    def __init__(self, args):
        super(TGAN, self).__init__()

        # define Generator net
        self.netG = UNet3D(input_channel=1, n_class=1)
        self.netD = UNet3D(input_channel=1, n_class=3)
        # define Discriminator net, n_class 3: 0 background, 1 real tumor, 2 fake tumor
        self.netSeg = SwinUNETR(
            img_size=(args.roi_x, args.roi_y, args.roi_z),
            in_channels=args.in_channels,
            out_channels=2,
            feature_size=args.feature_size,
            drop_rate=0.0,
            attn_drop_rate=0.0,
            dropout_path_rate=0.0,
            use_checkpoint=True,
            use_v2=True
        )
        self.init_netSeg(args)

        # initialize model inference
        inf_size = [args.roi_x, args.roi_y, args.roi_z]
        self.model_infererG = partial(
            sliding_window_inference,
            roi_size=inf_size,
            sw_batch_size=args.sw_batch_size,
            predictor=self.netG,
            overlap=args.infer_overlap,
        )
        self.model_infererSeg = partial(
            sliding_window_inference,
            roi_size=inf_size,
            sw_batch_size=args.sw_batch_size,
            predictor=self.netSeg,
            overlap=args.infer_overlap,
        )

        # defined by CVPR 2023 paper
        self.textures = []
        sigma_as = [3, 6, 9, 12, 15]
        sigma_bs = [4, 7]
        predefined_texture_shape = (420, 300, 320)
        if args.rank == 0 or args.distributed is False:
            print("Begin generate predefined texture.")
        for sigma_a in sigma_as:
            for sigma_b in sigma_bs:
                texture = get_predefined_texture(predefined_texture_shape, sigma_a, sigma_b)
                self.textures.append(texture)
        self.textures = self.textures.tolist()
        if args.rank == 0 or args.distributed is False:
            print("All predefined texture have generated.")
        self.args = args

    def init_netSeg(self, args):
        try:
            model_dict = torch.load(args.baseline_seg_dir,
                                    map_location=torch.device('cpu'))
            state_dict = model_dict["state_dict"]

            if "module." in list(state_dict.keys())[0]:
                print("Tag 'module.' found in state dict - fixing!")
                for key in list(state_dict.keys()):
                    state_dict[key.replace("module.", "")] = state_dict.pop(key)
            if "swin_vit" in list(state_dict.keys())[0]:
                print("Tag 'swin_vit' found in state dict - fixing!")
                for key in list(state_dict.keys()):
                    state_dict[key.replace("swin_vit", "swinViT")] = state_dict.pop(key)
            self.netSeg.load_state_dict(state_dict, strict=True)
            print("Using pretrained Swin UNETR backbone weights for netD!")
        except ValueError:
            raise ValueError("Pre-trained weights not available for" + str(args.model_name))

        self.netSeg.eval()

    def forward(self, image, label, mode, args):
        if mode == "losses_G":
            # 1. Generate difference
            difference = self.netG(image)
            difference = F.sigmoid(difference)

            fake_image, fake_label = self.Synthesis(image, label, difference)

            # 2. Discriminator: fake_tumor or real_tumor
            fake_loss = self.compute_fake_loss(fake_image, fake_label)

            # 3. Segmentation for Discriminator
            seg_loss, dice_acc = self.compute_discri_loss(fake_image, fake_label)

            seg_logits = self.model_infererSeg(fake_image)
            _, _ = self.TuringTest(image, label, fake_image, fake_label, seg_logits, mode)

            return fake_loss, seg_loss, dice_acc

        if mode == "losses_D":
            # 1. Generate fake tumor, without gradients
            with torch.no_grad():
                difference = self.netG(image)
                difference = F.sigmoid(difference)
                fake_image, fake_label = self.Synthesis(image, label, difference)

            loss_D = self.compute_fake_loss(fake_image, fake_label, mode)
            return loss_D

        if mode == "generate":
            with torch.no_grad():
                difference = self.model_infererG(image)
                difference = F.sigmoid(difference)
                fake_image, fake_label = self.Synthesis(image, label, difference)

                # We want to merge the segmentation results to select the tumors that passing the Turing test
                seg_logits = self.model_infererSeg(fake_image)
                new_image, new_label = self.TuringTest(image, label, fake_image, fake_label, seg_logits, mode)
                dice_acc = self.compute_dice_acc(seg_logits, fake_label)

            return fake_image, fake_label, seg_logits, dice_acc, new_image, new_label

    def Synthesis(self, image, label, difference=None):
        b, _, x, y, z = label.size()
        new_images, new_labels = [], []
        for i in range(b):
            img = image[i, 0, :, :, :].clone()
            lab = label[i, 0, :, :, :].clone()
            # diff = difference[i, 0, :, :, :] if difference is not None else None
            if difference is not None:
                diff = difference[i, 0, :, :, :]
                diff = Vol_Gaussian_Blur(diff)
            else:
                diff = None

            # random select tumor types
            if self.args.data == 'covid':
                tumor_types = ['tiny', 'small', 'medium']
                tumor_prob = [0.35, 0.35, 0.3]
            else:
                tumor_types = ['tiny', 'small']
                tumor_prob = [0.5, 0.5]

            tumor_prob = np.array(tumor_prob)
            tumor_type = np.random.choice(tumor_types, p=tumor_prob.ravel())

            texture = random.choice(self.textures)
            img, lab = SynthesisTumor(args=self.args, volume_scan=img, mask_scan=lab, tumor_type=tumor_type, texture=texture, num_tumor=1, difference=diff)

            new_images.append(img.unsqueeze(0).unsqueeze(0))
            new_labels.append(lab.unsqueeze(0).unsqueeze(0))

        new_images = torch.concat(new_images, dim=0)
        new_labels = torch.concat(new_labels, dim=0)

        return new_images.cuda(), new_labels.cuda()

    def compute_fake_loss(self, fake_image, fake_label, mode='losses_G'):
        logits = self.netD(fake_image)
        logits = F.softmax(logits)
        # 3 channels: 0, background. 1, real tumor. 2, fake tumor
        real_tumor_logit = logits[:, 1, :, :, :].unsqueeze(1)
        fake_tumor_logit = logits[:, 2, :, :, :].unsqueeze(1)

        fake_tumor_mask = fake_label.clone()
        fake_tumor_mask[fake_label != 3] = 0
        fake_tumor_mask[fake_label == 3] = 1

        real_tumor_mask = fake_label.clone()
        real_tumor_mask[fake_label != 2] = 0
        real_tumor_mask[fake_label == 2] = 1

        if mode == 'losses_G':
            # we want the real_tumor_logit to 1 when fake tumor: recognize fake tumor as real tumor
            loss = (real_tumor_logit * fake_tumor_mask).sum() / (fake_tumor_mask.sum() + 1e-6)
            loss = 1 - loss

        else:
            # mode == 'losses_D'
            # we want the fake_tumor_logit to 1 when fake tumor
            loss_fake = (fake_tumor_logit * fake_tumor_mask).sum() / (fake_tumor_mask.sum() + 1e-6)
            loss_fake = 1 - loss_fake

            # we want the real_tumor_logit to 1 when real tumor
            loss_real = (real_tumor_logit * real_tumor_mask).sum() / (real_tumor_mask.sum() + 1e-6)
            loss_real = 1 - loss_real

            loss = (loss_real + loss_fake)/2
            loss = loss

        return loss

    def compute_discri_loss(self, fake_image, fake_label):
        # disciminate with seg
        seg_logits = self.netSeg(fake_image)

        # # Fake tumor mask
        fake_tumor_mask = fake_label.clone()
        fake_tumor_mask[fake_label != 3] = 0
        fake_tumor_mask[fake_label == 3] = 1

        # compute seg loss
        tumor_logit = seg_logits.softmax(1)[:, -1, :, :, :].unsqueeze(1)
        tumor_logit = (tumor_logit * fake_tumor_mask).sum() / (fake_tumor_mask.sum() + 1e-6)
        loss = 1 - tumor_logit

        # Calculate Dice ACC
        dice_acc = self.compute_dice_acc(seg_logits, fake_label)

        return loss, dice_acc

    def compute_dice_acc(self, logits, fake_label):
        # logits: output channel 2. 0: background, 1: covid
        # # tumor label
        tumor_label = fake_label.clone()

        tumor_label[fake_label < 2] = 0
        tumor_label[fake_label >= 2] = 1

        acc_func = DiceMetric(include_background=False, reduction=MetricReduction.MEAN, get_not_nans=True)
        post_label = AsDiscrete(to_onehot=2)
        post_pred = AsDiscrete(argmax=True, to_onehot=2)

        val_labels_list = decollate_batch(tumor_label)
        val_labels_convert = [post_label(val_label_tensor) for val_label_tensor in val_labels_list]

        val_outputs_list = decollate_batch(logits)
        val_output_convert = [post_pred(val_pred_tensor) for val_pred_tensor in val_outputs_list]

        acc_func.reset()
        acc_func(y_pred=val_output_convert, y=val_labels_convert)
        acc, not_nans = acc_func.aggregate()

        return acc[0]

    def TuringTest(self, image, label, fake_image, fake_label, seg_logits, mode='generate'):
        # # Transform tumor label
        pred = seg_logits.argmax(1).unsqueeze(1).data
        pred_tumor = pred.clone()
        # pred: output channel 2. 0: background, 1: covid
        pred_tumor[pred == 1] = 1
        pred_tumor[pred != 1] = 0

        fake_tumor_mask = fake_label.clone()
        fake_tumor_mask[fake_label == 3] = 1
        fake_tumor_mask[fake_label != 3] = 0

        # The fake tumor that passed the Turing test !!!
        fake_tumor_mask_passTuring = fake_tumor_mask * pred_tumor.data

        proportion = fake_tumor_mask_passTuring.sum()/(fake_tumor_mask.sum() + 1e-6)
        if mode != 'generate':
            # if generate, don't show
            if self.args.rank == 0 or self.args.distributed is False:
                print('fake tumor pred proportion:', proportion.item())

        if proportion > 0.7:
            trans_label = fake_label.clone()
            trans_label[fake_label == 3] = 2
            return fake_image, trans_label
        else:
            return image, label


def Vol_Gaussian_Blur(vol, ks=7, blur_sigma=1):
    import cv2
    # 3D convolution
    vol_in = vol.reshape(1, 1, *vol.shape)
    k = torch.from_numpy(cv2.getGaussianKernel(ks, blur_sigma)).squeeze().float().to(vol.device)
    k3d = torch.einsum('i,j,k->ijk', k, k, k)
    k3d = k3d / k3d.sum()
    vol_3d = F.conv3d(vol_in, k3d.reshape(1, 1, *k3d.shape), stride=1, padding=len(k) // 2)
    vol = vol_3d[0][0]

    # Separable 1D convolution
    # k1d = k.view(1, 1, -1)
    # for _ in range(3):
    #     vol = F.conv1d(vol.reshape(-1, 1, vol.size(2)), k1d, padding=ks // 2).view(*vol.shape)
    #     vol = vol.permute(2, 0, 1)
    return vol