import os
import cPickle
from math import floor
import fire
import tensorflow as tf
from tensorflow.python.platform import tf_logging as logging
from model import *
from utils import *
from sklearn.metrics import accuracy_score

GPU_ID = 0
os.environ['CUDA_VISIBLE_DEVICES'] = str(GPU_ID)

logging.set_verbosity(logging.INFO)
# Basic model parameters as external flags.
flags = tf.app.flags
FLAGS = flags.FLAGS


class Options(object):
    def __init__(self):
        self.from_scratch = True
        self.unfrozen = True
        self.max_seq_len = 305
        self.embed = None
        self.embed_dim = 300

        self.filter_shape = 5
        self.filter_size = 300
        self.stride = [2, 2]
        self.multiplier = 1
        self.n_gan = 500
        self.L = 100
        self.cnn_layer_dropout = False
        self.mlp_hidden_dim = 300
        self.train = True

        self.optimizer = 'Adam'
        self.learning_rate = 2e-4
        self.clip_gradients = None
        self.batch_size = 32
        self.num_epochs = 100
        self.batch_norm = False
        self.dropout = True
        self.dropout_keep_prob = 0.5
        self.patience = 5

        self.padded_seq_len = self.max_seq_len + 2 * (self.filter_shape - 1)

        self.cnn_output_sizes = [self.padded_seq_len]
        for i in range(len(self.stride)):
            self.cnn_output_sizes.append(np.int32(floor(
                (self.cnn_output_sizes[i] - self.filter_shape) / self.stride[i]) + 1))

        self.log_path = None
        self.save_path = None
        self.print_freq = None
        self.valid_freq = None

    def __iter__(self):
        for attr, value in self.__dict__.iteritems():
            yield attr, value


def classifier(p, s, y, dropout_keep_prob, opts):
    p_embed, W_normalized = token_embedder(p, opts)
    s_embed = tf.nn.embedding_lookup(W_normalized, s)

    p_embed = tf.expand_dims(p_embed, 3)
    s_embed = tf.expand_dims(s_embed, 3)

    h_p = CNN(p_embed, opts, train=opts.train)
    h_s = CNN(s_embed, opts, train=opts.train, reuse=True)

    h_p = tf.squeeze(h_p)
    h_s = tf.squeeze(h_s)
    h = tf.concat([h_p, h_s], 1)

    logits = MLP(h, opts, keep_prob=dropout_keep_prob, prefix='classify', reuse=None)
    loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=y, logits=logits))

    prob = tf.nn.sigmoid(logits)
    correct_prediction = tf.equal(tf.round(prob), tf.round(y))
    accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))

    tf.summary.scalar('loss', loss)
    summaries = [
        "loss"
    ]
    global_step = tf.Variable(0, trainable=False)
    train_op = layers.optimize_loss(
        loss,
        global_step=global_step,
        optimizer=opts.optimizer,
        learning_rate=opts.learning_rate,
        summaries=summaries
    )
    return loss, train_op, prob, accuracy


def train_model(opts, x_train, y_train, x_val, y_val):
    with tf.device('/gpu:{}'.format(str(GPU_ID))):
        input_p = tf.placeholder(tf.int32, [opts.batch_size, opts.padded_seq_len], name='input_p')
        input_s = tf.placeholder(tf.int32, shape=[opts.batch_size, opts.padded_seq_len], name='input_s')
        input_y = tf.placeholder(tf.float32, shape=[opts.batch_size, 1], name='input_y')
        dropout_keep_prob = tf.placeholder(tf.float32, name='dropout_keep_prob')
        loss, train_op, prob, acc = classifier(input_p, input_s, input_y, dropout_keep_prob, opts)
        merged = tf.summary.merge_all()

    train_step = 0
    max_val_acc = 0.0
    best_epoch = 0
    config = tf.ConfigProto(log_device_placement=False, allow_soft_placement=True)
    config.gpu_options.allow_growth = True
    np.set_printoptions(precision=3)
    np.set_printoptions(threshold=np.inf)
    saver = tf.train.Saver()

    with tf.Session(config=config) as sess:
        train_writer = tf.summary.FileWriter(os.path.join(opts.log_path, 'train'), sess.graph)
        test_writer = tf.summary.FileWriter(os.path.join(opts.log_path, 'test'), sess.graph)
        sess.run(tf.global_variables_initializer())

        for epoch in range(opts.num_epochs):
            tf.logging.info('Epoch {}\n'.format(epoch))
            train_batches = get_minibatches(len(x_train), opts.batch_size, shuffle=True)
            for _, train_batch in train_batches:
                train_step += 1
                p_train_batch = [x_train[i, 0] for i in train_batch]
                s_train_batch = [x_train[i, 1] for i in train_batch]
                y_train_batch = [y_train[i] for i in train_batch]
                p_train_batch = prepare_data_for_CNN(p_train_batch, opts)
                s_train_batch = prepare_data_for_CNN(s_train_batch, opts)
                y_train_batch = np.array(y_train_batch).reshape((-1, 1))

                _, train_loss, _ = sess.run([train_op, loss, acc],
                                            feed_dict={input_p: p_train_batch,
                                                       input_s: s_train_batch,
                                                       input_y: y_train_batch,
                                                       dropout_keep_prob: opts.dropout_keep_prob})
                summary = sess.run(merged, feed_dict={input_p: p_train_batch,
                                                      input_s: s_train_batch,
                                                      input_y: y_train_batch,
                                                      dropout_keep_prob: opts.dropout_keep_prob})
                train_writer.add_summary(summary, train_step)

                if train_step % opts.print_freq == 0:
                    tf.logging.info('Train: step {}, loss {}'.format(train_step, train_loss))
                if train_step % opts.valid_freq == 0:
                    opts.train = False
                    val_batches = get_minibatches(len(x_val), opts.batch_size, shuffle=False)
                    val_probs = []
                    for _, val_batch in val_batches:
                        p_val_batch = [x_val[i, 0] for i in val_batch]
                        s_val_batch = [x_val[i, 1] for i in val_batch]
                        y_val_batch = [y_val[i] for i in val_batch]
                        p_val_batch = prepare_data_for_CNN(p_val_batch, opts)
                        s_val_batch = prepare_data_for_CNN(s_val_batch, opts)
                        y_val_batch = np.array(y_val_batch).reshape((-1, 1))

                        val_probs_batch = sess.run(prob, feed_dict={input_p: p_val_batch,
                                                                    input_s: s_val_batch,
                                                                    dropout_keep_prob: 1.0})
                        for p in val_probs_batch:
                            val_probs.append(p)

                    val_true = []
                    val_pred = []
                    for i in range(len(val_probs)):
                        if val_probs[i] > 0.5:
                            val_pred.append(1)
                        else:
                            val_pred.append(0)
                        val_true.append(y_val[i])

                    val_acc = accuracy_score(val_true, val_pred)
                    tf.logging.info('Val: accuracy {}'.format(val_acc))
                    summary = sess.run(merged, feed_dict={input_p: p_val_batch,
                                                          input_s: s_val_batch,
                                                          input_y: y_val_batch,
                                                          dropout_keep_prob: 1.0})
                    test_writer.add_summary(summary, train_step)

                    if val_acc >= max_val_acc:
                        max_val_acc = val_acc
                        best_epoch = epoch
                        saver.save(sess, os.path.join(opts.save_path, 'model'), global_step=epoch)
                        tf.logging.info('Epoch {} : max val accuracy {}, best epoch {}'.
                                        format(epoch, max_val_acc, best_epoch))
                    opts.train = True

            if epoch - best_epoch > opts.patience:
                break


def main(data_path, log_path, save_path, from_scratch, unfrozen,
         embed_dim, output_dim, learning_rate, batch_size, num_epochs, dropout_keep_prob,
         patience=5, print_freq=100, valid_freq=1000):
    # loading data
    data = cPickle.load(open(data_path, 'rb'))
    x_train, x_val = data[0], data[1]
    y_train, y_val = data[3], data[4]
    index2token = data[7]

    # randomly shuffle data
    x_train, y_train = shuffle_data(x_train, y_train)
    x_val, y_val = shuffle_data(x_val, y_val)

    opts = Options()
    opts.vocab_size = len(index2token)
    opts.log_path = log_path
    opts.save_path = save_path
    opts.from_scratch = from_scratch
    opts.unfrozen = unfrozen
    opts.embed_dim = embed_dim
    opts.n_gan = output_dim
    opts.learning_rate = learning_rate
    opts.batch_size = batch_size
    opts.num_epochs = num_epochs
    opts.dropout_keep_prob = dropout_keep_prob
    opts.patience = patience
    opts.print_freq = print_freq
    opts.valid_freq = valid_freq

    train_model(opts, x_train, y_train, x_val, y_val)
