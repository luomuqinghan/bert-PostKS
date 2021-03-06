import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from utils import gumbel_softmax
from copy import deepcopy
from pytorch_pretrained_bert import BertModel


model = BertModel.from_pretrained('bert-base-uncased')


class Encoder(nn.Module):
    def __init__(self):
        super(Encoder, self).__init__()
        self.encoder = deepcopy(model)

    def forward(self, X, mask):
        outputs, hidden = self.encoder(X, attention_mask=mask, output_all_encoded_layers=False)
        return outputs, hidden  # outputs: [n_batch, seq_len, n_hidden], # hidden: [n_batch, n_hidden]


class KnowledgeEncoder(nn.Module):
    def __init__(self, n_hidden):
        super(KnowledgeEncoder, self).__init__()
        self.n_hidden = n_hidden
        self.encoder = deepcopy(model)

    def forward(self, K):
        if len(K.shape) == 3:  # [n_batch, N, seq_len]
            n_batch = K.size(0)
            N = K.size(1)
            K = K.transpose(0, 1)  # [N, n_batch, seq_len]
            encoded = torch.zeros(N, n_batch, self.n_hidden)
            for i in range(N):
                mask = (K[i] != 0).long()
                _, hidden = self.encoder(K[i], attention_mask=mask, output_all_encoded_layers=False)
                encoded[i] = hidden
            return encoded.transpose(0, 1).cuda()  # [n_batch, N, n_hidden]

        else:  # [n_batch, seq_len]
            y = K
            mask = (y != 0).long()
            _, encoded = self.encoder(y, attention_mask=mask, output_all_encoded_layers=False)
            return encoded


class Manager(nn.Module):
    def __init__(self, n_hidden, n_vocab, temperature):
        super(Manager, self).__init__()
        self.n_hidden = n_hidden
        self.n_vocab = n_vocab
        self.temperature = temperature
        self.mlp = nn.Sequential(nn.Linear(2*n_hidden, n_hidden))
        self.mlp_k = nn.Sequential(nn.Linear(n_hidden, n_vocab))

    def forward(self, x, y, K):
        '''
        :param x:
            encoded utterance in shape (B, H)
        :param y:
            encoded response in shape (B, H) (optional)
        :param K:
            encoded knowledge in shape (B, N, H)
        :return:
            prior, posterior, selected knowledge, selected knowledge logits for BOW_loss
        '''
        if y is not None:
            prior = F.log_softmax(torch.bmm(x.unsqueeze(1), K.transpose(-1, -2)), dim=-1).squeeze(1)
            response = self.mlp(torch.cat((x, y), dim=-1))  # response: [n_batch, 2*n_hidden]
            K = K.transpose(-1, -2)  # K: [n_batch, n_hidden, N]
            posterior_logits = torch.bmm(response.unsqueeze(1), K).squeeze(1)
            posterior = F.softmax(posterior_logits, dim=-1)
            k_idx = gumbel_softmax(posterior_logits, self.temperature)  # k_idx: [n_batch, N(one_hot)]
            k_i = torch.bmm(K, k_idx.unsqueeze(2)).squeeze(2)  # k_i: [n_batch, n_hidden]
            k_logits = F.log_softmax(self.mlp_k(k_i), dim=-1)  # k_logits: [n_batch, n_vocab]
            return prior, posterior, k_i, k_logits  # prior: [n_batch, N], posterior: [n_batch, N]
        else:
            n_batch = K.size(0)
            k_i = torch.Tensor(n_batch, self.n_hidden).cuda()
            prior = torch.bmm(x.unsqueeze(1), K.transpose(-1, -2)).squeeze(1)
            k_idx = prior.max(1)[1].unsqueeze(1)  # k_idx: [n_batch, 1]
            for i in range(n_batch):
                k_i[i] = K[i, k_idx[i]]
            return k_i


class Attention(nn.Module):
    def __init__(self, n_hidden):
        super(Attention, self).__init__()
        self.attn = nn.Linear(2 * n_hidden, n_hidden)
        self.v = nn.Parameter(torch.rand(n_hidden))
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.uniform_(-stdv, stdv)

    def forward(self, hidden, encoder_outputs, encoder_mask=None):  # hidden: [n_batch, n_hidden]
        seq_len = encoder_outputs.size(1)  # encoder_outputs: [n_batch, seq_len, n_hidden]
        h = hidden.repeat(seq_len, 1, 1).transpose(0, 1)  # [n_batch, seq_len, n_hidden]
        attn_weights = self.score(h, encoder_outputs, encoder_mask)  # [n_batch, 1, seq_len]
        return attn_weights

    def score(self, hidden, encoder_outputs, encoder_mask=None):
        # hidden: [n_batch, seq_len, n_hidden], encoder_outputs: [n_batch, seq_len, n_hidden]
        attn_scores = torch.tanh(self.attn(torch.cat((hidden, encoder_outputs), dim=-1)))

        # attn_scores: [n_batch, seq_len, n_hidden]
        v = self.v.repeat(encoder_outputs.size(0), 1).unsqueeze(1)  # [n_batch, 1, n_hidden]
        attn_scores = torch.bmm(v, attn_scores.transpose(1, 2))  # [n_batch, 1, seq_len]
        if encoder_mask is not None:
            attn_scores.masked_fill_(encoder_mask, -1e9)
        attn_weights = F.softmax(attn_scores, dim=-1)  # [n_batch, 1, seq_len]
        return attn_weights  # [n_batch, 1, seq_len]


class Decoder(nn.Module):  # Hierarchical Gated Fusion Unit
    def __init__(self, n_hidden, n_embed, n_vocab):
        super(Decoder, self).__init__()
        self.n_hidden = n_hidden
        self.n_embed = n_embed
        self.n_vocab = n_vocab
        self.embedding = nn.Embedding(n_vocab, n_embed)
        self.attention = Attention(n_hidden)
        self.gru = nn.GRU(n_embed + n_hidden, n_hidden)
        self.out = nn.Linear(2 * n_hidden, n_vocab)

    def forward(self, input, k, hidden, encoder_outputs, encoder_mask=None):
        '''
        :param input:
            word_input for current time step, in shape (B)
        :param k:
            selected knowledge in shape (B, H)
        :param hidden:
            last hidden state of the decoder, in shape (1, B, H)
        :param encoder_outputs:
            encoder outputs in shape (B, T, H)
        :param encoder_mask:
            encoder mask in shape (B, 1, T)
        :return:
            decoder output, next hidden state of the decoder, attention weights
        '''
        embedded = self.embedding(input).unsqueeze(0)  # [1, n_batch, n_embed]
        attn_weights = self.attention(hidden, encoder_outputs, encoder_mask)  # [n_batch, 1, seq_len]
        context = torch.bmm(attn_weights, encoder_outputs)  # [n_batch, 1, n_hidden]
        context = context.transpose(0, 1)  # [1, n_batch, n_hidden]
        rnn_input = torch.cat((embedded, context), dim=-1)
        output, hidden = self.gru(rnn_input, hidden)  # hidden: [1, n_batch, n_hidden]
        output = output.squeeze(0)
        context = context.squeeze(0)  # [n_batch, n_hidden]
        output = self.out(torch.cat((output, context), dim=1))  # [n_batch, n_vocab]
        output = F.log_softmax(output, dim=1)
        return output, hidden, attn_weights
