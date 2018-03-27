# Decoder based models
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import random
from hier_class.models.sublayers import DocumentLevelAttention
import numpy as np
import time
import pdb


class SimpleDecoder(nn.Module):
    """
    Simple hierarchical decoder. Like fastText, it first encodes the document once,
    and then uses a GRU to predict the categories from a top-down approach.
    """

    def __init__(self, vocab_size=1, embedding_dim=300, cat_emb_dim=64, label_size=1, pad_token=1,
                 temperature=0.8,**kwargs):
        """

        :param vocab_size:
        :param embedding_dim:
        :param category_emb_dim:
        :param total_cats:
        :param label_size: number of total categories
        :param pad_token:
        :param kwargs:
        """
        super(SimpleDecoder, self).__init__()
        self.vocab_size = vocab_size
        self.label_size = label_size
        self.pad_token = pad_token
        self.category_emb = cat_emb_dim
        self.embedding_dim = embedding_dim
        self.temperature = temperature
        self.embedding = nn.Embedding(
            vocab_size,
            embedding_dim,
            pad_token
        )
        self.category_embedding = nn.Embedding(
            label_size,
            self.category_emb
        )
        self.decoder = nn.GRU(self.category_emb, embedding_dim, batch_first=True, dropout=0.5)
        #self.hidden2next = nn.Linear(embedding_dim*2, embedding_dim)
        self.decoder2linear = nn.Linear(embedding_dim, label_size)
        #self.logSoftMax = nn.LogSoftmax()
        #self.init_weights()

    def init_weights(self):
        initrange = 0.1
        self.embedding.weight.data.uniform(-initrange, initrange)
        nn.init.xavier_normal(
            self.decoder2linear.weight,
            gain=nn.init.calculate_gain('tanh')
        )

    def init_hidden(self, batch_size, gpu=0):
        hidden = Variable(torch.zeros(1, batch_size, self.embedding_dim)).cuda(gpu)
        return hidden

    def temp_logsoftmax(self, y, temperature):
        return F.log_softmax(y / temperature, dim=-1)


    def encode(self, src, src_lengths):
        """
        Encode the documents
        :param src: documents
        :param src_lengths: length of the documents
        :return:
        """
        src_emb = self.embedding(src)
        src_emb = torch.mean(src_emb,1)
        return src_emb

    def forward(self, categories, context_emb, hidden_state):
        """
        :param src: document to classify
        :param src_lengths: length of the documents
        :param num_cats: number of times to unroll
        :param categories: # keep starting category symbol as 0, such as
                        categories = torch.zeros(batch_size,1)
        :param context: embedding of context
        :return:
        """
        cat_emb = self.category_embedding(categories)

        #combined = torch.cat((cat_emb, context_emb.transpose(0,1)), -1)
        output, hidden_state = self.decoder(cat_emb, hidden_state)

        # combined = torch.cat((output, context_emb.transpose(0,1)), -1)
        # output = self.hidden2next(output)
        logits = self.decoder2linear(output)
        out = self.temp_logsoftmax(logits.view(-1, self.label_size), self.temperature)
        return out, hidden_state

    def batchNLLLoss(self, src, src_lengths, categories, tf_ratio=1.0, loss_focus=[],
                     loss_weights=None):
        """
        Calculate the negative log likelihood loss while predicting the categories
        :param src: documents to be classified
        :param src_lengths: length of the docs
        :param categories: hierarchical categories
        :param tf_ratio: teacher forcing ratio
        :return:
        """
        loss_fn = nn.NLLLoss(weight=loss_weights)
        loss = 0
        accs = []
        context_state = self.encode(src, src_lengths).unsqueeze(0)
        hidden_state = context_state #self.init_hidden(context_state.size(1))
        cat_len = categories.size(1) - 1
        #print("Categories : {}".format(cat_len))
        assert cat_len == 3
        out = None
        use_tf = True if (random.random() < tf_ratio) else False
        if use_tf:
            for i in range(cat_len):
                inp_cat = categories[:,i]
                inp_cat = inp_cat.unsqueeze(1)
                #hidden_state = torch.cat((hidden_state, context_state), 2)
                out, hidden_state = self.forward(inp_cat, context_state, hidden_state)
                target_cat = categories[:,i+1]
                loss += loss_fn(out, target_cat) * loss_focus[i]
                _, out_pred = torch.max(out.data, 1)
                acc = (out_pred == target_cat.data).sum() / len(target_cat)
                accs.append(acc)
        else:
            for i in range(cat_len):
                if i == 0:
                    inp_cat = categories[:,i].unsqueeze(1) # starting token
                else:
                    topv, topi = out.data.topk(1)
                    inp_cat = Variable(topi)
                #hidden_state = torch.cat((hidden_state, context_state), 2)
                out, hidden_state = self.forward(inp_cat, context_state, hidden_state)
                target_cat = categories[:, i+1]
                loss += loss_fn(out, target_cat) * loss_focus[i]
                _, out_pred = torch.max(out.data, 1)
                acc = (out_pred == target_cat.data).sum() / len(target_cat)
                accs.append(acc)

        return loss, accs

    def hierarchical_loss(self):
        """
        Calculate hierarchical loss. The idea is, if the current token is among improbable tokens,
        then return a high negative value. But it seems like this is an ideal case for REINFORCE
        :return:
        """
        pass


class SimpleMLPDecoder(nn.Module):
    """
    Simple hierarchical MLP decoder. Doesn't use a GRU, instead uses an MLP to classify per step.
    """

    def __init__(self, vocab_size=1, embedding_dim=300, cat_emb_dim=64, label_size=1,
                 label_sizes=[], label2id={},
                 total_cats=1,pad_token=1,
                 taxonomy=None,temperature=0.8, max_words=600,
                 d_k=64, d_v=64, n_head=1,
                 **kwargs):
        """

        :param vocab_size:
        :param embedding_dim:
        :param category_emb_dim:
        :param total_cats:
        :param label_size: number of total categories
        :param pad_token:
        :param taxonomy: hierarchy of labels
        :param kwargs:
        """
        super(SimpleMLPDecoder, self).__init__()
        self.vocab_size = vocab_size
        self.label_size = label_size
        self.label_sizes = label_sizes
        self.pad_token = pad_token
        self.embedding_dim = embedding_dim
        self.temperature = temperature
        self.label2id = label2id
        self.total_cats = total_cats
        self.embedding = nn.Embedding(
            vocab_size,
            embedding_dim,
            pad_token
        )
        # for attention to work, embedding_dim and category embedding dim should be the same
        if (embedding_dim * 2) != cat_emb_dim:
            print(embedding_dim)
            print(cat_emb_dim)
            raise RuntimeError("for attention to work, embedding_dim and category embedding dim should be the same or double for bidirectional")
        self.category_embedding = nn.Embedding(
            total_cats,
            cat_emb_dim
        )
        self.encoder = nn.LSTM(embedding_dim, embedding_dim, bidirectional=True, batch_first=True)
        self.linear = nn.Linear(embedding_dim * 2, label_size)
        #self.linear1 = nn.Linear(embedding_dim + cat_emb_dim, self.label_sizes[0])
        #self.linear2 = nn.Linear(embedding_dim + cat_emb_dim, self.label_sizes[1])
        #self.linear3 = nn.Linear(embedding_dim + cat_emb_dim, self.label_sizes[2])
        #self.linears = [self.linear1, self.linear2, self.linear3]
        self.taxonomy = taxonomy
        self.max_words = max_words
        self.attention = DocumentLevelAttention(embedding_dim,d_k,d_v)



    def init_weights(self):
        initrange = 0.1
        self.embedding.weight.data.uniform(-initrange, initrange)
        nn.init.xavier_normal(
            self.decoder2linear.weight,
            gain=nn.init.calculate_gain('tanh')
        )

    def init_hidden(self, batch_size, gpu=0):
        hidden = Variable(torch.zeros(batch_size, self.embedding_dim)).cuda(gpu)
        return hidden

    def temp_logsoftmax(self, y, temperature):
        return F.log_softmax(y / temperature, dim=-1)


    def encode(self, src, src_lengths):
        """
        Encode the documents
        :param src: documents
        :param src_lengths: length of the documents
        :return:
        """
        # TODO: use Attention - fix max doc length
        src_emb = self.embedding(src)
        #output = torch.mean(src_emb,1)
        src_pack = pack_padded_sequence(src_emb, src_lengths, batch_first=True)
        packed_output, (h_t, c_t) = self.encoder(src_pack)
        output, _ = pad_packed_sequence(packed_output, batch_first=True) # batch x seq x hid

        return output

    def attend(self, logits):
        pass

    def forward(self, encoder_outputs, inp_cat,level=0):
        """

        :param doc_emb:
        :param hidden_state:
        :return:
        """
        cat_emb = self.category_embedding(inp_cat)
        cat_emb = cat_emb.unsqueeze(1)
        doc_emb, attn = self.attention(cat_emb, encoder_outputs, encoder_outputs)
        #x = torch.cat((doc_emb, cat_emb), 1)
        doc_emb = doc_emb.squeeze(1)
        x = self.linear(doc_emb)
        #x = self.linears[level](x)
        logits = self.temp_logsoftmax(x, self.temperature)

        return logits

    def batchNLLLoss(self, src, src_lengths, categories, tf_ratio=1.0, loss_focus=[],
                     loss_weights=None, renormalize=False):
        """
        Calculate the negative log likelihood loss while predicting the categories
        :param src: documents to be classified
        :param src_lengths: length of the docs
        :param categories: hierarchical categories
        :param tf_ratio: teacher forcing ratio
        :return:
        """
        loss_fn = nn.NLLLoss(weight=loss_weights)
        #loss_fns = [nn.NLLLoss(), nn.NLLLoss(), nn.NLLLoss()]
        loss = 0
        accs = []
        encoder_outputs = self.encode(src, src_lengths)
        cat_len = categories.size(1) - 1
        assert cat_len == 3
        out = None
        out_p = None
        #pdb.set_trace()

        use_tf = True if (random.random() < tf_ratio) else False
        if use_tf:
            for i in range(cat_len):
                inp_cat = categories[:, i]
                if torch.max(inp_cat).data.cpu().numpy() > self.total_cats:
                    print(inp_cat)
                    raise RuntimeError("category ID outside of embedding")
                # hidden_state = torch.cat((hidden_state, context_state), 2)
                out = self.forward(encoder_outputs, inp_cat, i)
                if renormalize:
                    out = self.mask_renormalize(inp_cat, out)
                target_cat = categories[:, i+1]
                loss += loss_fn(out, target_cat) * loss_focus[i]
                #out_p = self.mask_renormalize(inp_cat, out.data)
                _, out_pred = torch.max(out.data, 1)
                acc = (out_pred == target_cat.data).sum() / len(target_cat)
                accs.append(acc)
        else:
            for i in range(cat_len):
                #pdb.set_trace()
                if i == 0:
                    inp_cat = categories[:, i]  # starting token
                else:
                    topv, topi = out.data.topk(1)
                    inp_cat = Variable(topi).squeeze(1)
                if torch.max(inp_cat).data.cpu().numpy() > self.total_cats:
                    print(inp_cat)
                    print(topi)
                    print(out.size())
                    raise RuntimeError("category ID outside of embedding")
                out = self.forward(encoder_outputs, inp_cat, i)
                if renormalize:
                    out = self.mask_renormalize(inp_cat, out)
                target_cat = categories[:, i + 1]
                loss += loss_fn(out, target_cat) * loss_focus[i]
                #out_p = self.mask_renormalize(inp_cat, out.data)
                _, out_pred = torch.max(out.data, 1)
                acc = (out_pred == target_cat.data).sum() / len(target_cat)
                accs.append(acc)

        return loss, accs

    def mask_renormalize(self, parent_class_batch, logits):
        """
        Given a parent class, logits and taxonomy, mask the classes which are not in child
        :param parent_class_batch: parent class ID in batch, batch x 1
        :param logits: batch x classes
        :return:
        """
        mask = torch.ones(logits.size())
        parent_class_batch = parent_class_batch.data.cpu().numpy()
        for batch_id, parent_class in enumerate(parent_class_batch):
            if parent_class in self.taxonomy:
                child_classes = self.taxonomy[parent_class]
                for cc in child_classes:
                    mask[batch_id][cc] = 0
        mask = mask.byte().cuda()
        logits.masked_fill_(mask, -float('inf'))
        return logits

    def label2category(self, label, level):
        return self.label2id['l{}_{}'.format(label, level)]







