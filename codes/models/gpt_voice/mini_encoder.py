import torch
import torch.nn as nn


from models.diffusion.nn import normalization, conv_nd, zero_module
from models.diffusion.unet_diffusion import Downsample, AttentionBlock, QKVAttention, QKVAttentionLegacy


# Combined resnet & full-attention encoder for converting an audio clip into an embedding.
from utils.util import checkpoint


class ResBlock(nn.Module):
    def __init__(
        self,
        channels,
        dropout,
        out_channels=None,
        use_conv=False,
        use_scale_shift_norm=False,
        dims=2,
        up=False,
        down=False,
        kernel_size=3,
        do_checkpoint=True,
    ):
        super().__init__()
        self.channels = channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_scale_shift_norm = use_scale_shift_norm
        self.do_checkpoint = do_checkpoint
        padding = 1 if kernel_size == 3 else 2

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            conv_nd(dims, channels, self.out_channels, kernel_size, padding=padding),
        )

        self.updown = up or down

        if up:
            self.h_upd = Upsample(channels, False, dims)
            self.x_upd = Upsample(channels, False, dims)
        elif down:
            self.h_upd = Downsample(channels, False, dims)
            self.x_upd = Downsample(channels, False, dims)
        else:
            self.h_upd = self.x_upd = nn.Identity()

        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(
                conv_nd(dims, self.out_channels, self.out_channels, kernel_size, padding=padding)
            ),
        )

        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        elif use_conv:
            self.skip_connection = conv_nd(
                dims, channels, self.out_channels, kernel_size, padding=padding
            )
        else:
            self.skip_connection = conv_nd(dims, channels, self.out_channels, 1)

    def forward(self, x):
        if self.do_checkpoint:
            return checkpoint(
                self._forward, x
            )
        else:
            return self._forward(x)

    def _forward(self, x):
        if self.updown:
            in_rest, in_conv = self.in_layers[:-1], self.in_layers[-1]
            h = in_rest(x)
            h = self.h_upd(h)
            x = self.x_upd(x)
            h = in_conv(h)
        else:
            h = self.in_layers(x)
        h = self.out_layers(h)
        return self.skip_connection(x) + h


class AudioMiniEncoder(nn.Module):
    def __init__(self, spec_dim, embedding_dim, resnet_blocks=2, attn_blocks=4, num_attn_heads=4, dropout=0):
        super().__init__()
        self.init = nn.Sequential(
            conv_nd(1, spec_dim, 128, 3, padding=1)
        )
        ch = 128
        res = []
        for l in range(2):
            for r in range(resnet_blocks):
                res.append(ResBlock(ch, dropout, dims=1, do_checkpoint=False))
            res.append(Downsample(ch, use_conv=True, dims=1, out_channels=ch*2, factor=2))
            ch *= 2
        self.res = nn.Sequential(*res)
        self.final = nn.Sequential(
            normalization(ch),
            nn.SiLU(),
            conv_nd(1, ch, embedding_dim, 1)
        )
        attn = []
        for a in range(attn_blocks):
            attn.append(AttentionBlock(embedding_dim, num_attn_heads, do_checkpoint=False))
        self.attn = nn.Sequential(*attn)

    def forward(self, x):
        h = self.init(x)
        h = self.res(h)
        h = self.final(h)
        h = self.attn(h)
        return h[:, :, 0]




class QueryProvidedAttentionBlock(nn.Module):
    """
    An attention block that provides a separate signal for the query vs the keys/parameters.
    """
    def __init__(
        self,
        channels,
        num_heads=1,
        num_head_channels=-1,
        use_new_attention_order=False,
    ):
        super().__init__()
        self.channels = channels
        if num_head_channels == -1:
            self.num_heads = num_heads
        else:
            assert (
                channels % num_head_channels == 0
            ), f"q,k,v channels {channels} is not divisible by num_head_channels {num_head_channels}"
            self.num_heads = channels // num_head_channels
        self.norm = normalization(channels)
        self.q = nn.Linear(channels, channels)
        self.qnorm = nn.LayerNorm(channels)
        self.kv = conv_nd(1, channels, channels*2, 1)
        if use_new_attention_order:
            # split qkv before split heads
            self.attention = QKVAttention(self.num_heads)
        else:
            # split heads before split qkv
            self.attention = QKVAttentionLegacy(self.num_heads)

        self.proj_out = zero_module(conv_nd(1, channels, channels, 1))

    def forward(self, qx, kvx, mask=None):
        return checkpoint(self._forward, qx, kvx, mask)

    def _forward(self, qx, kvx, mask=None):
        q = self.q(self.qnorm(qx)).unsqueeze(1).repeat(1, kvx.shape[1], 1).permute(0,2,1)
        kv = self.kv(self.norm(kvx.permute(0,2,1)))
        qkv = torch.cat([q, kv], dim=1)
        h = self.attention(qkv, mask)
        h = self.proj_out(h)
        return kvx + h.permute(0,2,1)


# Next up: combine multiple embeddings given a conditioning signal into a single embedding.
class EmbeddingCombiner(nn.Module):
    def __init__(self, embedding_dim, attn_blocks=3, num_attn_heads=2, cond_provided=True):
        super().__init__()
        block = QueryProvidedAttentionBlock if cond_provided else AttentionBlock
        self.attn = nn.ModuleList([block(embedding_dim, num_attn_heads) for _ in range(attn_blocks)])
        self.cond_provided = cond_provided

    # x_s: (b,n,d); b=batch_sz, n=number of embeddings, d=embedding_dim
    # cond: (b,d) or None
    def forward(self, x_s, attn_mask=None, cond=None):
        assert cond is not None and self.cond_provided or cond is None and not self.cond_provided
        y = x_s
        for blk in self.attn:
            if self.cond_provided:
                y = blk(cond, y, mask=attn_mask)
            else:
                y = blk(y, mask=attn_mask)
        return y[:, 0]


if __name__ == '__main__':
    x = torch.randn(2, 80, 223)
    cond = torch.randn(2, 512)
    encs = [AudioMiniEncoder(80, 512) for _ in range(5)]
    combiner = EmbeddingCombiner(512)

    e = torch.stack([e(x) for e in encs], dim=2)

    print(combiner(e, cond).shape)
