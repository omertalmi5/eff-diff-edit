import time
import os
import numpy as np
import torchvision.utils as tvu
import torchvision.transforms as tfs
import torch

from torch import nn
from pynvml import *
from PIL import Image

from models.ddpm.diffusion import DDPM
from models.improved_ddpm.script_util import i_DDPM
from utils.text_dic import SRC_TRG_TXT_DIC
from utils.diffusion_utils import get_beta_schedule, denoising_step
from losses import id_loss
from losses.clip_loss import CLIPLoss
from datasets.data_utils import get_dataset, get_dataloader
from utils.align_utils import run_alignment
from configs.paths_config import DATASET_PATHS, MODEL_PATHS


class EffDiff(object):
    def __init__(self, args, config, device=None):

        # ---------------------
        # Basic configurations
        self.args = args
        self.config = config
        if device is None:
            device = torch.device(
                "cuda:0") if torch.cuda.is_available() else torch.device("cpu")
        self.device = torch.device(device)
        # ---------------------

        # ---------------------
        # Diffusion settings
        self.model_var_type = config.model.var_type
        betas = get_beta_schedule(
            beta_start=config.diffusion.beta_start,
            beta_end=config.diffusion.beta_end,
            num_diffusion_timesteps=config.diffusion.num_diffusion_timesteps
        )
        self.betas = torch.from_numpy(betas).float()
        self.num_timesteps = betas.shape[0]

        alphas = 1.0 - betas
        alphas_cumprod = np.cumprod(alphas, axis=0)
        self.alphas_cumprod = alphas_cumprod
        alphas_cumprod_prev = np.append(1.0, alphas_cumprod[:-1])
        posterior_variance = betas * \
                             (1.0 - alphas_cumprod_prev) / (1.0 - alphas_cumprod)
        if self.model_var_type == "fixedlarge":
            self.logvar = np.log(np.append(posterior_variance[1], betas[1:]))

        elif self.model_var_type == 'fixedsmall':
            self.logvar = np.log(np.maximum(posterior_variance, 1e-20))

        self.betas = self.betas.to(self.device)
        self.logvar = torch.tensor(self.logvar).float().to(self.device)
        # ---------------------

        # ---------------------
        # Configuration of models,
        # optimizer, losses
        # and timestamps
        self._conf_model()
        self._conf_opt()
        self._conf_loss()
        self._conf_seqs()
        # ---------------------

        # ---------------------
        # Other stuff
        if self.args.edit_attr is None:
            self.src_txts = self.args.src_txts
            self.trg_image_paths = self.args.trg_txts
        else:
            self.src_txts = ["Hillary Clinton"]#SRC_TRG_TXT_DIC[self.args.edit_attr][0]
            self.trg_image_paths = ["joker.jpg"] #SRC_TRG_TXT_DIC[self.args.edit_attr][1]
            print("____________________________________________________")
            print(self.trg_image_paths)

        self.is_first = True
        self.is_first_train = True
        # ---------------------

    # Forward or backward processes of diffusion model
    # ----------------------------------------------------------------------------------
    def apply_diffusion(self,
                        x,
                        seq_prev,
                        seq_next,
                        eta=0.0,
                        sample_type='ddim',
                        is_one_step=False,
                        simple=False,
                        is_grad=False):
        if simple:
            t0 = self.args.t_0
            l1 = self.alphas_cumprod[t0]
            x = x * l1 ** 0.5 + (1 - l1) ** 0.5 * torch.randn_like(x)
            return x

        n = len(x)
        with torch.set_grad_enabled(is_grad):
            for it, (i, j) in enumerate(zip(seq_prev, seq_next)):
                t = (torch.ones(n) * i).to(self.device)
                t_prev = (torch.ones(n) * j).to(self.device)

                x, x0 = denoising_step(x,
                                       t=t,
                                       t_next=t_prev,
                                       models=self.model,
                                       logvars=self.logvar,
                                       sampling_type=sample_type,
                                       b=self.betas,
                                       eta=eta,
                                       out_x0_t=True,
                                       learn_sigma=self.learn_sigma)

                if is_one_step:
                    return x0

        return x
    # ----------------------------------------------------------------------------------

    # Computing latent variables
    # ----------------------------------------------------------------------------------
    @torch.no_grad()
    def precompute_latents(self):
        print("Prepare identity latent")

        self.img_lat_pairs_dic = {}

        for self.mode in ['train', 'test']:
            if self.mode == 'train':
                is_stoch = self.args.fast_noising_train
            else:
                is_stoch = self.args.fast_noising_test

            img_lat_pairs = []
            pairs_path = os.path.join('precomputed/',
                                      f'{self.config.data.category}_{self.mode}_t{self.args.t_0}_nim{self.args.n_precomp_img}_ninv{self.args.n_inv_step}_pairs.pth')

            # Loading latent variables if so exists
            # --------------------------------------------------
            print(pairs_path)
            if os.path.exists(pairs_path):
                print(f'{self.mode} pairs exists')
                self.img_lat_pairs_dic[self.mode] = torch.load(pairs_path)
                continue
            else:
                if self.args.own_training:
                    loader = os.listdir('imgs_for_train')
                    n_precomp_img = len(loader)
                else:
                    train_dataset, test_dataset = get_dataset(self.config.data.dataset, DATASET_PATHS, self.config)
                    loader_dic = get_dataloader(train_dataset, test_dataset, bs_train=self.args.bs_train,
                                                num_workers=self.config.data.num_workers)
                    loader = loader_dic[self.mode]
                    n_precomp_img = self.args.n_precomp_img
            # --------------------------------------------------

            # Preparation of the latents
            # --------------------------------------------------
            n_precomp = 0
            train_transform = tfs.Compose([tfs.ToTensor(),
                                           tfs.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5),
                                                          inplace=True)])

            if self.mode == 'test' and self.args.own_test != '0':
                if self.args.own_test == 'all':
                    loader = os.listdir('imgs_for_test')
                    n_precomp_img = len(loader)
                else:
                    loader = [self.args.own_test]
                    n_precomp_img = 1
            elif self.mode == 'test' and self.args.own_test == '0':
                n_precomp_img = self.args.n_precomp_img

            for self.step, img in enumerate(loader):

                # Configurations
                # --------------------------------
                if self.args.single_image:
                    if self.step != self.args.number_of_image:
                        continue
                    if self.args.own_test != '0':
                        img = train_transform(self._open_image(f"imgs_for_test/{self.args.own_test}"))
                        x0 = img.to(self.config.device).unsqueeze(0)
                    else:
                        x0 = img.to(self.config.device)
                else:
                    if self.mode == 'train' and self.args.own_training:
                        img = train_transform(self._open_image(f"imgs_for_train/{img}"))
                        x0 = img.to(self.config.device).unsqueeze(0)
                    elif self.mode == 'test' and (self.args.own_test != '0'):
                        img = train_transform(self._open_image(f"imgs_for_test/{img}"))
                        x0 = img.to(self.config.device).unsqueeze(0)
                    else:
                        x0 = img.to(self.config.device)
                # --------------------------------

                if self.args.single_image and self.mode == 'train':
                    self.save(x0, f'{self.mode}_{self.step}_0_orig.png')

                x = x0.clone()

                # Inversion of the real image
                x = self.apply_diffusion(x=x,
                                         seq_prev=self.seq_inv_next[1:],
                                         seq_next=self.seq_inv[1:],
                                         is_grad=False,
                                         simple=is_stoch)
                x_lat = x.clone()

                # Generation from computed latent variable
                x = self.apply_diffusion(x=x,
                                         seq_prev=reversed((self.seq_inv)),
                                         seq_next=reversed((self.seq_inv_next)),
                                         is_grad=False,
                                         is_one_step=True,
                                         sample_type=self.args.sample_type)

                img_lat_pairs.append([x0.detach().cpu(), x.detach().cpu().clone(), x_lat.detach().cpu().clone()])

                n_precomp += len(x)
                if n_precomp >= n_precomp_img:
                    break

            self.img_lat_pairs_dic[self.mode] = img_lat_pairs
            pairs_path = os.path.join('precomputed/',
                                      f'{self.config.data.category}_{self.mode}_t{self.args.t_0}_nim{self.args.n_precomp_img}_ninv{self.args.n_inv_step}_pairs.pth')
            torch.save(img_lat_pairs, pairs_path)
            # --------------------------------------------------

    # Fine tune the model
    # ----------------------------------------------------------------------------------
    def clip_finetune(self):
        print(self.args.exp)
        print(f'   {self.src_txts}')
        print(f'-> {self.trg_image_paths}')

        self.precompute_latents()

        print("Start finetuning")
        print(f"Sampling type: {self.args.sample_type.upper()} with eta {self.args.eta}")

        for self.src_txt, self.trg_image_path in zip(self.src_txts, self.trg_image_paths):
            print(f"CHANGE {self.src_txt} TO {self.trg_image_path}")

            self.clip_loss_func.target_direction = None

            for self.it_out in range(self.args.n_iter):

                # Single training steps
                self.mode = 'train'
                self.train()

                # Single evaluation step if needed
                if self.args.do_test and not self.args.single_image:
                    self.mode = 'test'
                    self.eval()

    # Single training epoch
    # ----------------------------------------------------------------------------------
    def train(self):
        for self.step, (x0, x_id, x_lat) in enumerate(self.img_lat_pairs_dic['train']):
            self.model.train()

            time_in_start = time.time()

            self.optim_ft.zero_grad()
            x = x_lat.clone().to(self.device)

            # Single step estimation of the real object
            x = self.apply_diffusion(x=x,
                                     seq_prev=reversed(self.seq_train),
                                     seq_next=reversed(self.seq_train_next),
                                     sample_type=self.args.sample_type,
                                     is_grad=True,
                                     eta=self.args.eta,
                                     is_one_step=True)

            # Losses
            x_source = x0.to(self.device)
            train_transform = tfs.Compose([tfs.ToTensor(),
                                           tfs.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5),
                                                          inplace=True)])
            img_ref = train_transform(self._open_image(f"imgs_for_test/{self.trg_image_path}"))
            img_ref_vec = img_ref.to(self.config.device).unsqueeze(0)
            loss_clip = (2 - self.clip_loss_func(x_source, self.src_txt, x, img_ref_vec)) / 2
            loss_clip = -torch.log(loss_clip)
            loss_id = 0
            loss_l1 = nn.L1Loss()(x0.to(self.device), x)
            # loss = self.args.clip_loss_w * loss_clip + self.args.id_loss_w * loss_id + self.args.l1_loss_w * loss_l1
            loss = self.args.clip_loss_w * loss_clip + self.args.l1_loss_w * loss_l1

            loss.backward()

            self.optim_ft.step()
            time_in_end = time.time()

            print(f"CLIP {self.step}-{self.it_out}: loss_l1: {loss_l1:.3f}, loss_clip: {loss_clip:.3f}")
            print(f"Training for {len(x)} image(s) takes {time_in_end - time_in_start:.4f}s")

            if self.args.single_image:
                x = x_lat.clone().to(self.device)
                self.model.eval()
                x = self.apply_diffusion(x=x,
                                         seq_prev=reversed(self.seq_train),
                                         seq_next=reversed(self.seq_train_next),
                                         sample_type=self.args.sample_type,
                                         is_grad=False,
                                         eta=self.args.eta,
                                         is_one_step=False)
                self.save(x,
                          f'train_{self.step}_2_clip_{self.trg_image_path.replace(" ", "_")}_{self.it_out}_ngen{self.args.n_train_step}.png')

                if self.is_first_train:
                    self.save(x0, f'{self.mode}_{self.step}_0_orig.png')

            if self.step == self.args.n_train_img - 1:
                break

        self.scheduler_ft.step()
        self.is_first_train = False
    # ----------------------------------------------------------------------------------

    # Evaluation
    # ----------------------------------------------------------------------------------
    def eval(self):
        self.model.eval()
        for self.step, (x0, x_id, x_lat) in enumerate(self.img_lat_pairs_dic['test']):

            x = self.apply_diffusion(x=x_lat.to(self.device),
                                     seq_prev=reversed(self.seq_train),
                                     seq_next=reversed(self.seq_train_next),
                                     sample_type=self.args.sample_type,
                                     eta=self.args.eta,
                                     is_grad=False,
                                     is_one_step=False)

            if self.is_first:
                self.save(x0, f'{self.mode}_{self.step}_0_orig.png')

            print(f"Eval {self.step}-{self.it_out}")
            self.save(x,
                      f'test_{self.step}_2_clip_{self.trg_image_path.replace(" ", "_")}_{self.it_out}_ngen{self.args.n_test_step}.png')

            if self.step == self.args.n_test_img - 1:
                break

        self.is_first = False
    # ----------------------------------------------------------------------------------

    ####################################################################################
    # UTILS FUNCTIONS

    # Preparation of sequences
    # ----------------------------------------------------------------------------------
    def _conf_seqs(self):
        seq_inv = np.linspace(0, 1, self.args.n_inv_step) * self.args.t_0
        self.seq_inv = [int(s) for s in list(seq_inv)]
        self.seq_inv_next = [-1] + list(self.seq_inv[:-1])

        if self.args.n_train_step != 0:
            seq_train = np.linspace(0, 1, self.args.n_train_step) * self.args.t_0
            self.seq_train = [int(s) for s in list(seq_train)]
            print('Uniform skip type')
        else:
            self.seq_train = list(range(self.args.t_0))
            print('No skip')
        self.seq_train_next = [-1] + list(self.seq_train[:-1])

        self.seq_test = np.linspace(0, 1, self.args.n_test_step) * self.args.t_0
        self.seq_test = [int(s) for s in list(self.seq_test)]
        self.seq_test_next = [-1] + list(self.seq_test[:-1])
    # ----------------------------------------------------------------------------------

    # Configuration of the diffusion model
    # ----------------------------------------------------------------------------------
    def _conf_model(self):
        if self.config.data.dataset == "LSUN":
            if self.config.data.category == "bedroom":
                url = "https://image-editing-test-12345.s3-us-west-2.amazonaws.com/checkpoints/bedroom.ckpt"
            elif self.config.data.category == "church_outdoor":
                url = "https://image-editing-test-12345.s3-us-west-2.amazonaws.com/checkpoints/church_outdoor.ckpt"
        elif self.config.data.dataset == "CelebA_HQ":
            url = "https://huggingface.co/gwang-kim/DiffusionCLIP-CelebA_HQ/resolve/main/celeba_hq.ckpt"
        elif self.config.data.dataset == "AFHQ":
            pass
        elif self.config.data.dataset == "IMAGENET":
            pass
        else:
            raise ValueError

        if self.config.data.dataset in ["CelebA_HQ", "LSUN"]:
            model = DDPM(self.config)
            if self.args.model_path:
                init_ckpt = torch.load(self.args.model_path)
            else:
                init_ckpt = torch.hub.load_state_dict_from_url(url, map_location=self.device)
            self.learn_sigma = False
            print("Original diffusion Model loaded.")
        elif self.config.data.dataset in ["FFHQ", "AFHQ", "IMAGENET"]:
            model = i_DDPM(self.config.data.dataset)
            if self.args.model_path:
                init_ckpt = torch.load(self.args.model_path)
            else:
                init_ckpt = torch.load(MODEL_PATHS[self.config.data.dataset])
            self.learn_sigma = True
            print("Improved diffusion Model loaded.")
        else:
            print('Not implemented dataset')
            raise ValueError
        model.load_state_dict(init_ckpt)

        model.to(self.device)
        self.model = model
    # ----------------------------------------------------------------------------------

    # Configuration of the optimizer
    # ----------------------------------------------------------------------------------
    def _conf_opt(self):
        print(f"Setting optimizer with lr={self.args.lr_clip_finetune}")

        params_to_update = []
        for name, param in self.model.named_parameters():
            if param.requires_grad == True:
                params_to_update.append(param)

        self.optim_ft = torch.optim.Adam(params_to_update, weight_decay=0, lr=self.args.lr_clip_finetune)
        self.init_opt_ckpt = self.optim_ft.state_dict()
        self.scheduler_ft = torch.optim.lr_scheduler.StepLR(self.optim_ft, step_size=1, gamma=self.args.sch_gamma)
        self.init_sch_ckpt = self.scheduler_ft.state_dict()
    # ----------------------------------------------------------------------------------

    # Configuration of the loss
    # ----------------------------------------------------------------------------------
    def _conf_loss(self):
        print("Loading losses")
        self.clip_loss_func = CLIPLoss(
            self.device,
            lambda_direction=1,
            lambda_patch=0,
            lambda_global=0,
            lambda_manifold=0,
            lambda_texture=0,
            clip_model=self.args.clip_model_name)
        #self.id_loss_func = id_loss.IDLoss().to(self.device).eval()
    # ----------------------------------------------------------------------------------

    # ----------------------------------------------------------------------------------
    def _open_image(self, path):
        # change size first
        img = Image.open(path).convert('RGB').resize((256, 256))
        img.save(path)
        if self.args.align_face:
            try:
                img = run_alignment(path, output_size=self.config.data.image_size)
            except:
                img = Image.open(path).convert('RGB').resize((256, 256))

            return img
        else:
            img = Image.open(path).convert('RGB').resize((256, 256))
            return img
    # ----------------------------------------------------------------------------------

    @torch.no_grad()
    def save(self, x, name):
        tvu.save_image((x + 1) * 0.5, os.path.join(self.args.image_folder, name))

    ####################################################################################
