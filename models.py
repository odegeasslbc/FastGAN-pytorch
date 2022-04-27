# import torch
# import torch.nn as nn
# from torch.nn.utils import spectral_norm
# import torch.nn.functional as F

from random import randint
import tensorflow as tf
import tensorflow.keras as keras
from tensorflow.keras import layers
from tensorflow_addons.layers import SpectralNormalization, AdaptiveAveragePooling2D

# seq = nn.Sequential


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find("Conv") != -1:
        try:
            m.weight.data.normal_(0.0, 0.02)
        except:
            pass
    elif classname.find("BatchNorm") != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


def conv2d(*args, **kwargs):
    return SpectralNormalization(layers.Conv2D(*args, **kwargs))


def convTranspose2d(*args, **kwargs):
    return SpectralNormalization(layers.Conv2DTranspose(*args, **kwargs))


def batchNorm2d(*args, **kwargs):
    return layers.BatchNormalization(*args, **kwargs)


def linear(*args, **kwargs):
    return SpectralNormalization(layers.Dense(*args, **kwargs))


class PixelNorm(layers.Layer):
    def call(self, input):
        return input * tf.math.rsqrt(
            tf.math.reduce_mean(input ** 2, dim=1, keepdim=True) + 1e-8
        )


class Reshape(layers.Layer):
    def __init__(self, shape):
        super().__init__()
        self.target_shape = shape

    def call(self, feat):
        batch = feat.shape[0]
        return feat.reshape(batch, *self.target_shape)


class GLU(layers.Layer):
    def call(self, x):
        nc = x.size(1)
        assert nc % 2 == 0, "channels dont divide 2!"
        nc = int(nc / 2)
        return x[:, :nc] * tf.math.sigmoid(x[:, nc:])


class NoiseInjection(layers.Layer):
    def __init__(self):
        super().__init__()

    def build(self, _):
        self.weight = self.add_weight(
            "kernel", shape=(1,), initializer="zeros", trainable=True,
        )

    def call(self, feat, noise=None):
        if noise is None:
            batch, _, height, width = feat.shape
            noise = tf.random.normal((batch, 1, height, width))

        return feat + self.weight * noise


class Swish(layers.Layer):
    def call(self, feat):
        return feat * tf.math.sigmoid(feat)


class SEBlock(layers.Layer):
    def __init__(self, ch_in, ch_out):
        super().__init__()
        self.main = keras.Sequential(
            [
                AdaptiveAveragePooling2D(4),
                conv2d(ch_in, ch_out, 4, 1, 0, use_bias=False),
                Swish(),
                conv2d(ch_out, ch_out, 1, 1, 0, use_bias=False),
                layers.Activation("sigmoid"),
            ]
        )

    def call(self, feat_small, feat_big):
        return feat_big * self.main(feat_small)


class InitLayer(layers.Layer):
    def __init__(self, nz, channel):
        super().__init__()
        self.init = keras.Sequential(
            [
                convTranspose2d(nz, channel * 2, 4, 1, 0, use_bias=False),
                batchNorm2d(channel * 2),
                GLU(),
            ]
        )

    def call(self, noise):
        noise = noise.reshape(noise.shape[0], -1, 1, 1)
        return self.init(noise)


def UpBlock(in_planes, out_planes):
    block = keras.Sequential(
        [
            layers.UpSampling2D(scale_factor=2, interpolation="nearest"),
            conv2d(in_planes, out_planes * 2, 3, 1, 1, use_bias=False),
            # convTranspose2d(in_planes, out_planes*2, 4, 2, 1, use_bias=False),
            batchNorm2d(out_planes * 2),
            GLU(),
        ]
    )
    return block


def UpBlockComp(in_planes, out_planes):
    block = keras.Sequential(
        [
            layers.UpSampling2D(scale_factor=2, interpolation="nearest"),
            conv2d(in_planes, out_planes * 2, 3, 1, 1, use_bias=False),
            # convTranspose2d(in_planes, out_planes*2, 4, 2, 1, use_bias=False),
            NoiseInjection(),
            batchNorm2d(out_planes * 2),
            GLU(),
            conv2d(out_planes, out_planes * 2, 3, 1, 1, use_bias=False),
            NoiseInjection(),
            batchNorm2d(out_planes * 2),
            GLU(),
        ]
    )
    return block


class Generator(keras.Model):
    def __init__(self, ngf=64, nz=100, nc=3, im_size=1024):
        super(Generator, self).__init__()

        nfc_multi = {
            4: 16,
            8: 8,
            16: 4,
            32: 2,
            64: 2,
            128: 1,
            256: 0.5,
            512: 0.25,
            1024: 0.125,
        }
        nfc = {}
        for k, v in nfc_multi.items():
            nfc[k] = int(v * ngf)

        self.im_size = im_size

        self.init = InitLayer(nz, channel=nfc[4])

        self.feat_8 = UpBlockComp(nfc[4], nfc[8])
        self.feat_16 = UpBlock(nfc[8], nfc[16])
        self.feat_32 = UpBlockComp(nfc[16], nfc[32])
        self.feat_64 = UpBlock(nfc[32], nfc[64])
        self.feat_128 = UpBlockComp(nfc[64], nfc[128])
        self.feat_256 = UpBlock(nfc[128], nfc[256])

        self.se_64 = SEBlock(nfc[4], nfc[64])
        self.se_128 = SEBlock(nfc[8], nfc[128])
        self.se_256 = SEBlock(nfc[16], nfc[256])

        self.to_128 = conv2d(nfc[128], nc, 1, 1, 0, use_bias=False)
        self.to_big = conv2d(nfc[im_size], nc, 3, 1, 1, use_bias=False)

        if im_size > 256:
            self.feat_512 = UpBlockComp(nfc[256], nfc[512])
            self.se_512 = SEBlock(nfc[32], nfc[512])
        if im_size > 512:
            self.feat_1024 = UpBlock(nfc[512], nfc[1024])

    def call(self, input):

        feat_4 = self.init(input)
        feat_8 = self.feat_8(feat_4)
        feat_16 = self.feat_16(feat_8)
        feat_32 = self.feat_32(feat_16)

        feat_64 = self.se_64(feat_4, self.feat_64(feat_32))

        feat_128 = self.se_128(feat_8, self.feat_128(feat_64))

        feat_256 = self.se_256(feat_16, self.feat_256(feat_128))

        if self.im_size == 256:
            return [self.to_big(feat_256), self.to_128(feat_128)]

        feat_512 = self.se_512(feat_32, self.feat_512(feat_256))
        if self.im_size == 512:
            return [self.to_big(feat_512), self.to_128(feat_128)]

        feat_1024 = self.feat_1024(feat_512)

        im_128 = tf.math.tanh(self.to_128(feat_128))
        im_1024 = tf.math.tanh(self.to_big(feat_1024))

        return [im_1024, im_128]


class DownBlock(layers.Layer):
    def __init__(self, in_planes, out_planes):
        super(DownBlock, self).__init__()

        self.main = keras.Sequential(
            [
                conv2d(in_planes, out_planes, 4, 2, 1, use_bias=False),
                batchNorm2d(out_planes),
                layers.LeakyReLU(alpha=0.2),
            ]
        )

    def call(self, feat):
        return self.main(feat)


class DownBlockComp(layers.Layer):
    def __init__(self, in_planes, out_planes):
        super(DownBlockComp, self).__init__()

        self.main = keras.Sequential(
            [
                conv2d(in_planes, out_planes, 4, 2, 1, use_bias=False),
                batchNorm2d(out_planes),
                layers.LeakyReLU(alpha=0.2),
                conv2d(out_planes, out_planes, 3, 1, 1, use_bias=False),
                batchNorm2d(out_planes),
                layers.LeakyReLU(alpha=0.2),
            ]
        )

        self.direct = keras.Sequential(
            [
                layers.AveragePooling2D(pool_size=(2, 2)),
                conv2d(in_planes, out_planes, 1, 1, 0, use_bias=False),
                batchNorm2d(out_planes),
                layers.LeakyReLU(alpha=0.2),
            ]
        )

    def call(self, feat):
        return (self.main(feat) + self.direct(feat)) / 2


class Discriminator(keras.Model):
    def __init__(self, ndf=64, nc=3, im_size=512):
        super(Discriminator, self).__init__()
        self.ndf = ndf
        self.im_size = im_size

        nfc_multi = {
            4: 16,
            8: 16,
            16: 8,
            32: 4,
            64: 2,
            128: 1,
            256: 0.5,
            512: 0.25,
            1024: 0.125,
        }
        nfc = {}
        for k, v in nfc_multi.items():
            nfc[k] = int(v * ndf)

        if im_size == 1024:
            self.down_from_big = keras.Sequential(
                [
                    conv2d(nc, nfc[1024], 4, 2, 1, use_bias=False),
                    layers.LeakyReLU(alpha=0.2),
                    conv2d(nfc[1024], nfc[512], 4, 2, 1, use_bias=False),
                    batchNorm2d(nfc[512]),
                    layers.LeakyReLU(alpha=0.2),
                ]
            )
        elif im_size == 512:
            self.down_from_big = keras.Sequential(
                [conv2d(nc, nfc[512], 4, 2, 1, use_bias=False), layers.LeakyReLU(alpha=0.2)]
            )
        elif im_size == 256:
            self.down_from_big = keras.Sequential(
                [conv2d(nc, nfc[512], 3, 1, 1, use_bias=False), layers.LeakyReLU(alpha=0.2)]
            )

        self.down_4 = DownBlockComp(nfc[512], nfc[256])
        self.down_8 = DownBlockComp(nfc[256], nfc[128])
        self.down_16 = DownBlockComp(nfc[128], nfc[64])
        self.down_32 = DownBlockComp(nfc[64], nfc[32])
        self.down_64 = DownBlockComp(nfc[32], nfc[16])

        self.rf_big = keras.Sequential(
            [
                conv2d(nfc[16], nfc[8], 1, 1, 0, use_bias=False),
                batchNorm2d(nfc[8]),
                layers.LeakyReLU(alpha=0.2),
                conv2d(nfc[8], 1, 4, 1, 0, use_bias=False),
            ]
        )

        self.se_2_16 = SEBlock(nfc[512], nfc[64])
        self.se_4_32 = SEBlock(nfc[256], nfc[32])
        self.se_8_64 = SEBlock(nfc[128], nfc[16])

        self.down_from_small = keras.Sequential(
            [
                conv2d(nc, nfc[256], 4, 2, 1, use_bias=False),
                layers.LeakyReLU(alpha=0.2),
                DownBlock(nfc[256], nfc[128]),
                DownBlock(nfc[128], nfc[64]),
                DownBlock(nfc[64], nfc[32]),
            ]
        )

        self.rf_small = conv2d(nfc[32], 1, 4, 1, 0, use_bias=False)

        self.decoder_big = SimpleDecoder(nfc[16], nc)
        self.decoder_part = SimpleDecoder(nfc[32], nc)
        self.decoder_small = SimpleDecoder(nfc[32], nc)

    def call(self, imgs, label, part=None):
        if type(imgs) is not list:
            imgs = [
                tf.image.resize(imgs, (self.im_size, self.im_size), method="nearest"),
                tf.image.resize(imgs, (128, 128), method="nearest"),
            ]

        feat_2 = self.down_from_big(imgs[0])
        feat_4 = self.down_4(feat_2)
        feat_8 = self.down_8(feat_4)

        feat_16 = self.down_16(feat_8)
        feat_16 = self.se_2_16(feat_2, feat_16)

        feat_32 = self.down_32(feat_16)
        feat_32 = self.se_4_32(feat_4, feat_32)

        feat_last = self.down_64(feat_32)
        feat_last = self.se_8_64(feat_8, feat_last)

        # rf_0 = tf.concat([self.rf_big_1(feat_last).reshape(-1),self.rf_big_2(feat_last).reshape(-1)])
        # rff_big = torch.sigmoid(self.rf_factor_big)
        rf_0 = self.rf_big(feat_last).reshape(-1)

        feat_small = self.down_from_small(imgs[1])
        # rf_1 = tf.concat([self.rf_small_1(feat_small).reshape(-1),self.rf_small_2(feat_small).reshape(-1)])
        rf_1 = self.rf_small(feat_small).reshape(-1)

        if label == "real":
            rec_img_big = self.decoder_big(feat_last)
            rec_img_small = self.decoder_small(feat_small)

            assert part is not None
            rec_img_part = None
            if part == 0:
                rec_img_part = self.decoder_part(feat_32[:, :, :8, :8])
            if part == 1:
                rec_img_part = self.decoder_part(feat_32[:, :, :8, 8:])
            if part == 2:
                rec_img_part = self.decoder_part(feat_32[:, :, 8:, :8])
            if part == 3:
                rec_img_part = self.decoder_part(feat_32[:, :, 8:, 8:])

            return tf.concat([rf_0, rf_1]), [rec_img_big, rec_img_small, rec_img_part]

        return tf.concat([rf_0, rf_1])


class SimpleDecoder(layers.Layer):
    """docstring for CAN_SimpleDecoder"""

    def __init__(self, nfc_in=64, nc=3):
        super(SimpleDecoder, self).__init__()

        nfc_multi = {
            4: 16,
            8: 8,
            16: 4,
            32: 2,
            64: 2,
            128: 1,
            256: 0.5,
            512: 0.25,
            1024: 0.125,
        }
        nfc = {}
        for k, v in nfc_multi.items():
            nfc[k] = int(v * 32)

        def upBlock(in_planes, out_planes):
            block = keras.Sequential(
                layers.UpSampling2D(scale_factor=2, interpolation="nearest"),
                conv2d(in_planes, out_planes * 2, 3, 1, 1, use_bias=False),
                batchNorm2d(out_planes * 2),
                GLU(),
            )
            return block

        self.main = keras.Sequential(
            [
                AdaptiveAveragePooling2D(8),
                upBlock(nfc_in, nfc[16]),
                upBlock(nfc[16], nfc[32]),
                upBlock(nfc[32], nfc[64]),
                upBlock(nfc[64], nfc[128]),
                conv2d(nfc[128], nc, 3, 1, 1, use_bias=False),
                layers.Activation("tanh"),
            ]
        )

    def call(self, input):
        # input shape: c x 4 x 4
        return self.main(input)


def random_crop(image, size):
    h, w = image.shape[2:]
    ch = randint(0, h - size - 1)
    cw = randint(0, w - size - 1)
    return image[:, :, ch : ch + size, cw : cw + size]


class TextureDiscriminator(layers.Layer):
    def __init__(self, ndf=64, nc=3, im_size=512):
        super(TextureDiscriminator, self).__init__()
        self.ndf = ndf
        self.im_size = im_size

        nfc_multi = {
            4: 16,
            8: 8,
            16: 8,
            32: 4,
            64: 2,
            128: 1,
            256: 0.5,
            512: 0.25,
            1024: 0.125,
        }
        nfc = {}
        for k, v in nfc_multi.items():
            nfc[k] = int(v * ndf)

        self.down_from_small = keras.Sequential(
            [
                conv2d(nc, nfc[256], 4, 2, 1, use_bias=False),
                layers.LeakyReLU(alpha=0.2),
                DownBlock(nfc[256], nfc[128]),
                DownBlock(nfc[128], nfc[64]),
                DownBlock(nfc[64], nfc[32]),
            ]
        )
        self.rf_small = conv2d(nfc[16], 1, 4, 1, 0, use_bias=False)

        self.decoder_small = SimpleDecoder(nfc[32], nc)

    def call(self, img, label):
        img = random_crop(img, size=128)

        feat_small = self.down_from_small(img)
        rf = self.rf_small(feat_small).reshape(-1)

        if label == "real":
            rec_img_small = self.decoder_small(feat_small)

            return rf, rec_img_small, img

        return rf
