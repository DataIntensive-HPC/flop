import os
import sys
import argparse
import time
import random

import numpy as np
import torch
import torch.nn as nn
from torch.optim import Adam
from tensorboardX import SummaryWriter

import sru
import flop


def read_corpus(path, num_test_symbols=5000000):
    raw_data = open(path).read()
    raw_data = np.fromstring(raw_data, dtype=np.uint8)
    unique, data = np.unique(raw_data, return_inverse=True)
    train_data = data[: -2 * num_test_symbols]
    valid_data = data[-2 * num_test_symbols : -num_test_symbols]
    test_data = data[-num_test_symbols:]
    return train_data, valid_data, test_data, unique


def create_batches(data_ids, batch_size):
    N = len(data_ids)
    L = ((N - 1) // batch_size) * batch_size
    x = np.copy(data_ids[:L].reshape(batch_size, -1).T)
    y = np.copy(data_ids[1 : L + 1].reshape(batch_size, -1).T)
    x, y = torch.from_numpy(x), torch.from_numpy(y)
    x, y = x.contiguous(), y.contiguous()
    x, y = x.cuda(), y.cuda()
    return x, y


class CustomLinear(nn.Linear):
    def __init__(self, in_features, out_features, bias=False):
        super(CustomLinear, self).__init__(in_features, out_features, bias=bias)

    def forward(self, data, **kwargs):
        return super().forward(data)


class Model(nn.Module):
    def __init__(self, words, args):
        super(Model, self).__init__()
        self.args = args
        if args.n_e:
            self.n_e = args.n_e
        else:
            self.n_e = len(words) if len(words) < args.n_d else args.n_d
        self.n_d = args.n_d
        self.depth = args.depth
        self.drop = nn.Dropout(args.dropout)
        self.embedding_layer = nn.Embedding(len(words), self.n_e)
        self.n_V = len(words)
        custom_m_list = [CustomLinear(self.n_e, self.n_d * 4, bias=False)]
        for i in range(self.depth - 1):
            custom_m_list.append(
                flop.ProjectedLinear(
                    self.n_d, self.n_d * 3, proj_features=args.n_proj, bias=False
                )
            )
        self.rnn = sru.SRU(
            self.n_e,
            self.n_d,
            self.depth,
            dropout=args.dropout,
            highway_bias=args.bias,
            layer_norm=args.layer_norm,
            rescale=args.rescale,
            custom_m=custom_m_list,
        )
        self.output_layer = nn.Linear(self.n_d, self.n_V)
        self.init_weights()

    def init_weights(self, reinit_rnn=False):
        params = list(self.embedding_layer.parameters()) + list(
            self.output_layer.parameters()
        )
        for p in params:
            if p.dim() > 1:  # matrix
                val = (3.0 / p.size(0)) ** 0.5
                p.data.uniform_(-val, val)
            else:
                p.data.zero_()
        if reinit_rnn:
            for p in self.rnn.parameters():
                if p.dim() > 1:  # matrix
                    val = (3.0 / p.size(0)) ** 0.5
                    p.data.uniform_(-val, val)

    def forward(self, x, hidden):
        emb = self.drop(self.embedding_layer(x))
        output, hidden = self.rnn(emb, hidden)
        output = self.drop(output)
        output = output.view(-1, output.size(2))
        output = self.output_layer(output)
        return output, hidden

    def init_hidden(self, batch_size):
        weight = next(self.parameters()).data
        zeros = weight.new(self.depth, batch_size, self.n_d).zero_()
        return zeros


def calc_norm(lis):
    l2_sum = sum(x.norm() ** 2 for x in lis)
    return l2_sum ** 0.5


def eval_model(model, valid):
    with torch.no_grad():
        model.eval()
        args = model.args
        batch_size = valid[0].size(1)
        total_loss = 0.0
        unroll_size = args.unroll_size
        criterion = nn.CrossEntropyLoss(size_average=False)
        hidden = model.init_hidden(batch_size)
        N = (len(valid[0]) - 1) // unroll_size + 1
        for i in range(N):
            x = valid[0][i * unroll_size : (i + 1) * unroll_size]
            y = valid[1][i * unroll_size : (i + 1) * unroll_size].view(-1)
            hidden.detach_()
            output, hidden = model(x, hidden)
            loss = criterion(output, y)
            total_loss += loss.item()
        avg_loss = total_loss / valid[1].numel()
        ppl = np.exp(avg_loss)
        model.train()
        return ppl, avg_loss


def copy_model(model):
    states = model.state_dict()
    for k in states:
        v = states[k]
        states[k] = v.clone().cpu()
    return states


def main(args):
    log_path = "{}_{}".format(args.log, random.randint(1, 100))
    train_writer = SummaryWriter(log_dir=log_path + "/train")
    dev_writer = SummaryWriter(log_dir=log_path + "/dev")

    train, dev, test, words = read_corpus(args.data)
    dev_, test_ = dev, test
    train = create_batches(train, args.batch_size)
    dev = create_batches(test, args.batch_size)
    test = create_batches(test, args.batch_size)

    model = Model(words, args)
    if args.load:
        model.load_state_dict(torch.load(args.load))
    model.cuda()
    print(model)
    print("vocab size: {}".format(model.n_V))

    lr = 1.0 if not args.noam else 1.0 / (args.n_d ** 0.5) / (args.warmup_steps ** 1.5)
    if args.prune:
        model.cuda()
        print(model)
        num_mask_params = sum(x.numel() for x in model.rnn.parameters())
        num_prunable_params = num_mask_params
        print("num of mask parameters: {}".format(num_mask_params))
        print("num of prunable parameters: {}".format(num_prunable_params))
        param_names = [
            i[0]
            for i in model.rnn.named_parameters()
            if i[1].requires_grad
        ]
        pruner = flop.NervanaPruner(
            model.rnn,
            subpruners={
                "agppruner": {
                    "class": "AutomatedGradualPruner",
                    "initial_sparsity": 0.05,
                    "weights": param_names,
                    "final_sparsity": args.prune_sparsity,
                    "starting_step": args.prune_start_epoch,
                    "ending_step": args.prune_end_epoch,
                    "frequency": 1,
                }
            },
        )
    else:
        args.prune_start_epoch = args.max_epoch

    m_parameters = [
        i[1]
        for i in model.named_parameters()
        if i[1].requires_grad
    ]
    optimizer = Adam(m_parameters, lr=lr * args.lr, weight_decay=args.weight_decay)
    num_params = sum(x.numel() for x in m_parameters if x.requires_grad)
    print("num of parameters: {}".format(num_params))

    nbatch = 1
    niter = 1
    best_dev = 1e8
    unroll_size = args.unroll_size
    batch_size = args.batch_size
    N = (len(train[0]) - 1) // unroll_size + 1
    criterion = nn.CrossEntropyLoss()

    model.zero_grad()

    for epoch in range(args.max_epoch):
        start_time = time.time()
        model.train()
        hidden = model.init_hidden(batch_size)

        pruner.begin_step(epoch)
        for i in range(N):
            # start iter on the first batch
            if nbatch % args.update_param_freq == 1:
                pruner.begin_iter(epoch, niter, N // args.update_param_freq)

            x = train[0][i * unroll_size : (i + 1) * unroll_size]
            y = train[1][i * unroll_size : (i + 1) * unroll_size].view(-1)
            hidden.detach_()

            # language model forward and backward
            output, hidden = model(x, hidden)
            model_loss = criterion(output, y)

            loss = model_loss
            (loss / args.update_param_freq).backward()
            model_loss = model_loss.item()

            #  log training stats
            if (niter - 1) % 100 == 0 and nbatch % args.update_param_freq == 0:
                sys.stderr.write(
                    "\r{:.4f}".format(
                        model_loss,
                    )
                )
                train_writer.add_scalar("loss/lm_loss", model_loss, niter)
                train_writer.add_scalar(
                    "parameter_norm", calc_norm([x.data for x in m_parameters]), niter
                )
                train_writer.add_scalar(
                    "gradient_norm",
                    calc_norm([x.grad for x in m_parameters if x.grad is not None]),
                    niter,
                )

            #  perform gradient decent every few number of backward()
            if nbatch % args.update_param_freq == 0:
                if args.clip_grad > 0:
                    torch.nn.utils.clip_grad_norm(m_parameters, args.clip_grad)
                optimizer.step()
                #  clear gradient
                model.zero_grad()

                # End iter on the last batch
                pruner.end_iter(epoch, niter, N // args.update_param_freq)
                niter += 1

            if nbatch % args.log_period == 0 or i == N - 1:
                elapsed_time = (time.time() - start_time) / 60.0
                dev_ppl, dev_loss = eval_model(model, dev)
                dev_writer.add_scalar("loss/lm_loss", dev_loss, niter)
                dev_writer.add_scalar("bpc", np.log2(dev_ppl), niter)
                sparsity = 0
                if args.prune:
                    agp_sparsity = pruner.get_step_logs()['sparsity']
                    dev_writer.add_scalar("sparsity/hard_sparsity", agp_sparsity, niter)
                    # dev_writer.add_scalar("sparsity/agp_sparsity", agp_sparsity, niter)
                    dev_writer.add_scalar(
                        "model_size/total_prunable", num_prunable_params, niter
                    )
                    dev_writer.add_scalar("model_size/total", num_params, niter)
                sys.stdout.write(
                    "\rIter={}  lr={:.5f}  train_loss={:.4f}  dev_loss={:.4f}"
                    "  dev_bpc={:.2f}  sparsity={:.2f}\teta={:.1f}m\t[{:.1f}m]\n".format(
                        niter,
                        optimizer.param_groups[0]["lr"],
                        loss,
                        dev_loss,
                        np.log2(dev_ppl),
                        sparsity,
                        elapsed_time * N / (i + 1),
                        elapsed_time,
                    )
                )

                checkpoint = copy_model(model)
                sys.stdout.write("\n")
                sys.stdout.flush()

            nbatch += 1
            if args.noam:
                lr = min(1.0 / (niter ** 0.5), niter / (args.warmup_steps ** 1.5))
                optimizer.param_groups[0]["lr"] = lr * args.lr / (args.n_d ** 0.5)

        pruner.end_step(epoch)
        if args.save and (epoch + 1) % 10 == 0:
            torch.save(copy_model(model), "{}.{}.{:.3f}.pt".format(
                args.save,
                epoch + 1,
                sparsity
            ))

    train_writer.close()
    dev_writer.close()

    model.load_state_dict(checkpoint)
    model.cuda()
    dev = create_batches(dev_, 1)
    test = create_batches(test_, 1)
    dev_ppl, dev_loss = eval_model(model, dev)
    test_ppl, test_loss = eval_model(model, test)
    sys.stdout.write(
        "dev_bpc={:.3f}  test_bpc={:.3f}\n".format(np.log2(dev_ppl), np.log2(test_ppl))
    )


if __name__ == "__main__":
    argparser = argparse.ArgumentParser(sys.argv[0], conflict_handler="resolve")
    argparser.add_argument("--log", type=str, default="")
    argparser.add_argument("--noam", type=bool, default=True)
    argparser.add_argument("--warmup_steps", type=int, default=16000)
    argparser.add_argument("--layer_norm", action="store_true")
    argparser.add_argument("--rescale", action="store_true")
    argparser.add_argument("--data", type=str, required=True, help="training file")
    argparser.add_argument("--batch_size", "--batch", type=int, default=64)
    argparser.add_argument("--update_param_freq", type=int, default=1)
    argparser.add_argument("--unroll_size", type=int, default=256)
    argparser.add_argument("--max_epoch", type=int, default=100)
    argparser.add_argument("--n_e", type=int, default=0)
    argparser.add_argument("--n_d", "--d", type=int, default=3056)
    argparser.add_argument("--n_proj", type=int, default=512)
    argparser.add_argument(
        "--dropout", type=float, default=0.1, help="dropout probability"
    )
    argparser.add_argument(
        "--bias", type=float, default=-3, help="intial bias of highway gates",
    )
    argparser.add_argument("--depth", type=int, default=6)
    argparser.add_argument("--lr", type=float, default=2)
    argparser.add_argument("--weight_decay", type=float, default=1e-7)
    argparser.add_argument("--clip_grad", type=float, default=0.3)
    argparser.add_argument("--log_period", type=int, default=1000000)
    argparser.add_argument("--save", type=str, default="")
    argparser.add_argument("--load", type=str, default="")

    argparser.add_argument("--prune", type=bool, default=True)
    argparser.add_argument("--prune_lr", type=float, default=2)
    argparser.add_argument("--prune_warmup", type=int, default=0)
    argparser.add_argument("--prune_start_epoch", type=int, default=0)
    argparser.add_argument("--prune_sparsity", type=float, default=0.9)
    argparser.add_argument("--prune_end_epoch", type=int, default=30)
    argparser.add_argument("--l1_lambda", type=float, default=0)

    args = argparser.parse_args()
    main(args)
