import argparse
import time
import numpy as np
import networkx as nx
import torch
import torch.nn as nn
import torch.nn.functional as F
import dgl
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
from dgl.data import register_data_args, load_data
from dgl.nn.pytorch.conv import ChebConv, GMMConv
from dgl.nn.pytorch.glob import MaxPooling
from grid_graph import grid_graph
from coarsening import coarsen
from coordinate import get_coordinates, z2polar

torch.set_default_dtype(torch.float32)
argparser = argparse.ArgumentParser("MNIST")
argparser.add_argument(
    "--gpu", type=int, default=-1, help="gpu id, use cpu if set to -1"
)
argparser.add_argument(
    "--model", type=str, default="chebnet", help="model to use, chebnet/monet"
)
argparser.add_argument("--batch-size", type=int, default=100, help="batch size")
args = argparser.parse_args()

grid_side = 28  # 255 #28
number_edges = 8
metric = "euclidean"

A = grid_graph(grid_side, 8, metric)
# A = grid_graph(28, 8, metric)

coarsening_levels = 4
L, perm = coarsen(A, coarsening_levels)
g_arr = [dgl.from_scipy(csr) for csr in L]

coordinate_arr = get_coordinates(g_arr, grid_side, coarsening_levels, perm)
for g, coordinate_arr in zip(g_arr, coordinate_arr):
    g.ndata["xy"] = coordinate_arr
    g.apply_edges(z2polar)


def batcher(batch):
    g_batch = [[] for _ in range(coarsening_levels + 1)]
    x_batch = []
    y_batch = []
    for x, y in batch:
        x = torch.cat([x.view(-1), x.new_zeros(len(perm) - 28 ** 2)], 0)
        x = x[perm]
        x_batch.append(x)
        y_batch.append(y)
        for i in range(coarsening_levels + 1):
            g_batch[i].append(g_arr[i])

    x_batch = torch.cat(x_batch).unsqueeze(-1)
    y_batch = torch.LongTensor(y_batch)
    g_batch = [dgl.batch(g) for g in g_batch]
    return g_batch, x_batch, y_batch


"""
trainset = datasets.MNIST(
    root=".", train=True, download=True, transform=transforms.ToTensor()
)
testset = datasets.MNIST(
    root=".", train=False, download=True, transform=transforms.ToTensor()
)

train_loader = DataLoader(
    trainset,
    batch_size=args.batch_size,
    shuffle=True,
    collate_fn=batcher,
    num_workers=6,
)
test_loader = DataLoader(
    testset,
    batch_size=args.batch_size,
    shuffle=False,
    collate_fn=batcher,
    num_workers=6,
)
"""

######################################################################
import torchvision.transforms as transforms

transform = transforms.Compose(
    [
        transforms.Resize(255),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]
)


train_path = "/home/knc6/Software/atomvision/atomvision/data/STM_JV/train_folder"
test_path = "/home/knc6/Software/atomvision/atomvision/data/STM_JV/test_folder"


train_path = "/home/knc6/Software/atomvision/atomvision/data/STEM_JV/train_folder"
test_path = "/home/knc6/Software/atomvision/atomvision/data/STEM_JV/test_folder"


train_dataset = datasets.ImageFolder(
    train_path,
    transform=transform,
)
test_dataset = datasets.ImageFolder(
    test_path,
    transform=transform,
)
# val_set = train_set #datasets.ImageFolder("root/label/valid", transform = transformations)

# test_ratio=0.2
# n_train=int((1-test_ratio)*len(dataset))
# n_test=len(dataset)-n_train
# print (len(dataset),n_train,n_test)
# train_set, val_set = torch.utils.data.random_split(dataset, [n_train,n_test])
# Put into a Dataloader using torch library


train_loader = torch.utils.data.DataLoader(
    train_dataset, batch_size=32, collate_fn=batcher, shuffle=True
)
test_loader = torch.utils.data.DataLoader(
    test_dataset, batch_size=32, collate_fn=batcher, shuffle=True
)


######################################################################


class MoNet(nn.Module):
    def __init__(self, n_kernels, in_feats, hiddens, out_feats):
        super(MoNet, self).__init__()
        self.pool = nn.MaxPool1d(2)
        self.layers = nn.ModuleList()
        self.readout = MaxPooling()

        # Input layer
        self.layers.append(GMMConv(in_feats, hiddens[0], 2, n_kernels))

        # Hidden layer
        for i in range(1, len(hiddens)):
            self.layers.append(GMMConv(hiddens[i - 1], hiddens[i], 2, n_kernels))

        self.cls = nn.Sequential(nn.Linear(hiddens[-1], out_feats), nn.LogSoftmax())

    def forward(self, g_arr, feat):
        for g, layer in zip(g_arr, self.layers):
            u = g.edata["u"]
            feat = (
                self.pool(layer(g, feat, u).transpose(-1, -2).unsqueeze(0))
                .squeeze(0)
                .transpose(-1, -2)
            )
            print(feat.shape)
        print(g_arr[-1].batch_size)
        return self.cls(self.readout(g_arr[-1], feat))


class ChebNet(nn.Module):
    def __init__(self, k, in_feats, hiddens, out_feats):
        super(ChebNet, self).__init__()
        self.pool = nn.MaxPool1d(2)
        self.layers = nn.ModuleList()
        self.readout = MaxPooling()

        # Input layer
        self.layers.append(ChebConv(in_feats, hiddens[0], k))

        for i in range(1, len(hiddens)):
            self.layers.append(ChebConv(hiddens[i - 1], hiddens[i], k))

        self.cls = nn.Sequential(nn.Linear(hiddens[-1], out_feats), nn.LogSoftmax())

    def forward(self, g_arr, feat):
        for g, layer in zip(g_arr, self.layers):
            feat = (
                self.pool(
                    layer(g, feat, [2] * g.batch_size).transpose(-1, -2).unsqueeze(0)
                )
                .squeeze(0)
                .transpose(-1, -2)
            )
        return self.cls(self.readout(g_arr[-1], feat))


if args.gpu == -1:
    device = torch.device("cpu")
else:
    device = torch.device(args.gpu)

if args.model == "chebnet":
    model = ChebNet(2, 1, [32, 64, 128, 256], 5)
else:
    model = MoNet(10, 1, [32, 64, 128, 256], 5)

model = model.to(device)

optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
log_interval = 50
nepochs = 100
for epoch in range(nepochs):
    print("epoch {} starts".format(epoch))
    model.train()
    hit, tot = 0, 0
    loss_accum = 0
    # for i, (g, y) in enumerate(train_loader):
    for i, (g, x, y) in enumerate(train_loader):
        # print ('g',g)
        # print ('x',x)
        # print ('y',y)
        # print ()
        x = x.to(device)
        y = y.to(device)
        g = [g_i.to(device) for g_i in g]
        out = model(g, x)
        # out = model(g, x)
        hit += (out.max(-1)[1] == y).sum().item()
        tot += len(y)
        loss = F.nll_loss(out, y)
        loss_accum += loss.item()

        if (i + 1) % log_interval == 0:
            print("loss: {}, acc: {}".format(loss_accum / log_interval, hit / tot))
            hit, tot = 0, 0
            loss_accum = 0

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    model.eval()
    hit, tot = 0, 0
    for g, x, y in test_loader:
        x = x.to(device)
        y = y.to(device)
        out = model(g, x)
        hit += (out.max(-1)[1] == y).sum().item()
        tot += len(y)

    print("test acc: ", hit / tot)
