"""
@Description: Sampling-Bias-Corrected Neural Model
@version: 
@License: MIT
@Author: Wang Yao
@Date: 2020-08-26 20:47:47
@LastEditors: Wang Yao
@LastEditTime: 2020-09-27 17:17:42
"""
import os
import time
import functools
import numpy as np
import tensorflow as tf

from src.embedding.google_tt.modeling import build_model


def parse_csv_line(left_columns,
                   right_columns,
                   csv_header,
                   csv_line):
    """解析csv数据"""
    
    def _parse_columns(columns):
        columns_defs = []
        columns_indexs = []
        for col in columns:
            columns_indexs.append(csv_header.index(col))
            if "time" in col or "count" in col:
                columns_defs.append(tf.constant(-1, dtype=tf.int32))
            else:
                columns_defs.append(tf.constant(""))
    
        parsed_columns = tf.io.decode_csv(
            csv_line,
            record_defaults=columns_defs,
            select_cols=columns_indexs,
            use_quote_delim=False
        )
        return parsed_columns

    def _parse_multi_hot(tensor, max_length=10, duplicate=True, delim='_'):
        """Multi-hot"""
        vals = tensor.numpy().decode('utf-8').strip(delim).split(delim)
        if duplicate is True:
            vals = list(set(vals))
        if len(vals) < max_length:
            vals = vals + ['']* (max_length-len(vals))
        if len(vals) > max_length:
            vals = vals[: max_length]
        return tf.constant(vals)

    left_parsed_columns = _parse_columns(left_columns)
    right_parsed_columns = _parse_columns(right_columns)
    labels = _parse_columns(['label'])
    
    left_features = dict(zip(left_columns, left_parsed_columns))
    right_features = dict(zip(right_columns, right_parsed_columns))
    left_features['past_watches'] = tf.py_function(
        func=functools.partial(_parse_multi_hot, max_length=10, duplicate=False), 
        inp=[left_features['past_watches']], 
        Tout=[tf.string])[0]
    left_features['seed_tags'] = tf.py_function(
        func=functools.partial(_parse_multi_hot, max_length=5, duplicate=True), 
        inp=[left_features['seed_tags']], 
        Tout=[tf.string])[0]
    right_features['cand_tags'] = tf.py_function(
        func=functools.partial(_parse_multi_hot, max_length=5, duplicate=True), 
        inp=[right_features['cand_tags']], 
        Tout=[tf.string])[0]
    labels = tf.py_function(
        func=functools.partial(_parse_multi_hot, max_length=5, duplicate=False), 
        inp=[labels[0]], 
        Tout=[tf.string])
    weight_labels = tf.math.reduce_sum(
        tf.math.multiply(tf.strings.to_number(labels), 
        tf.constant([0.5, 0.2, 0.1, 0.2, 0.]))) # 点击、点赞、分享、收藏、评论

    return left_features, right_features, weight_labels


def get_dataset_from_csv_files(filenames,
                               left_columns,
                               right_columns,
                               csv_header,
                               batch_size=256,
                               epochs=None,
                               shuffle_size=None):
    """消费csv文件列表"""
    list_ds = tf.data.Dataset.list_files(filenames)
    dataset = list_ds.interleave(
        lambda fp: tf.data.TextLineDataset(fp).skip(1),
        cycle_length=2,
        block_length=batch_size*2,
        num_parallel_calls=2
    )
    dataset = dataset.map(
        map_func=functools.partial(
            parse_csv_line, 
            left_columns,
            right_columns,
            csv_header),
        num_parallel_calls=2
    )
    dataset = dataset.cache()
    if epochs is not None:
        dataset = dataset.repeat(epochs)
    if shuffle_size is not None:
        dataset = dataset.shuffle(shuffle_size)
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(batch_size)
    return dataset


def sampling_p_estimation_single_hash(array_a, array_b, hash_indexs, global_step, alpha=0.01):
    """单Hash函数采样概率估计"""
    array_b[hash_indexs] = (1 - alpha) * array_b[hash_indexs] + \
                                alpha * (global_step - array_a[hash_indexs])
    array_a[hash_indexs] = global_step
    sampling_p = 1 / array_b[hash_indexs]
    return array_a, array_b, sampling_p


def hash_simple(ids, hash_bucket_size):
    """取余Hash函数"""
    if tf.keras.backend.dtype(ids) != 'int32':
        ids = tf.cast(ids, dtype=tf.int32)
    return tf.math.mod(ids, hash_bucket_size)


def log_q(x, y, sampling_p=None, temperature=0.05):
    """logQ correction used in sampled softmax model."""
    inner_product = tf.reduce_sum(tf.math.multiply(x, y), axis=-1) / temperature
    if sampling_p is not None:
        return inner_product - tf.math.log(sampling_p)
    return inner_product


def corrected_batch_softmax(x, y, sampling_p=None):
    """logQ correction softmax"""
    correct_inner_product = log_q(x, y, sampling_p=sampling_p)
    return tf.math.exp(correct_inner_product) / tf.math.reduce_sum(tf.math.exp(correct_inner_product))
    

def reward_cross_entropy(reward, output):
    """Reward correction batch """
    return -tf.reduce_mean(reward * tf.math.log(output))


def topk_recall(output, reward, k=10):
    """TopK Recall rate"""
    _, indices = tf.math.top_k(output, k=k)

    def _ture(reward, indices):
        return tf.math.count_nonzero(tf.gather(reward, indices)) / tf.math.count_nonzero(reward)
    
    def _false():
        return tf.constant(0., dtype=tf.float64)

    return tf.cond(tf.math.count_nonzero(reward) > 0, lambda: _ture(reward, indices), lambda: _false())


def topk_positive(output, reward, k=10):
    """Topk Positive rate"""
    _, indices = tf.math.top_k(output, k=k)

    def _ture(reward, indices):
        return tf.math.count_nonzero(tf.gather(reward, indices)) / k
    
    def _false():
        return tf.constant(0., dtype=tf.float64)

    return tf.cond(tf.math.count_nonzero(reward) > 0, lambda: _ture(reward, indices), lambda: _false())
    

def train_model(strategy,
                dataset, 
                steps,
                epochs,
                ids_column,
                ids_hash_bucket_size,
                tensorboard_dir=None,
                checkpoints_dir=None,
                streaming=False,
                beta=100,
                lr=0.001):
    """自定义训练"""

    dataset = strategy.experimental_distribute_dataset(dataset)

    with strategy.scope():
        left_model, right_model = build_model()

        def pred(left_x, right_x, sampling_p):
            left_y_ = left_model(left_x, training=True)
            right_y_ = right_model(right_x, training=True)
            output = corrected_batch_softmax(left_y_, right_y_, sampling_p=sampling_p)
            return output

        def loss(left_x, right_x, sampling_p, reward):
            output = pred(left_x, right_x, sampling_p)
            return reward_cross_entropy(reward, output)

        def grad(left_x, right_x, sampling_p, reward):
            with tf.GradientTape(persistent=True) as tape:
                loss_value = loss(left_x, right_x, sampling_p, reward)
            left_grads = tape.gradient(loss_value, left_model.trainable_variables)
            right_grads = tape.gradient(loss_value, right_model.trainable_variables)
            return loss_value, left_grads, right_grads

        epoch_recall_avg = tf.keras.metrics.Mean()
        epoch_positive_avg = tf.keras.metrics.Mean()

        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        left_checkpointer = tf.train.Checkpoint(optimizer=optimizer, model=left_model)
        right_checkpointer = tf.train.Checkpoint(optimizer=optimizer, model=right_model)

        left_checkpoint_prefix = os.path.join(checkpoints_dir, "left-ckpt")
        right_checkpoint_prefix = os.path.join(checkpoints_dir, "right-ckpt")

        def train_step(inputs, sampling_p):
            left_x, right_x, reward = inputs
            loss_value, left_grads, right_grads = grad(left_x, right_x, sampling_p, reward)
            optimizer.apply_gradients(zip(left_grads, left_model.trainable_variables))
            optimizer.apply_gradients(zip(right_grads, right_model.trainable_variables))

            epoch_recall_avg.update_state(topk_recall(pred(left_x, right_x, sampling_p), reward))
            epoch_positive_avg.update_state(topk_positive(pred(left_x, right_x, sampling_p), reward))

            return loss_value

        @tf.function
        def distributed_train_step(inputs, sampling_p=None):
            per_replica_losses = strategy.run(train_step, args=(inputs, sampling_p,))
            return strategy.reduce(tf.distribute.ReduceOp.MEAN, per_replica_losses, axis=None)
            
        loss_results = []
        recall_results = []
        positive_results = []
        
        if tensorboard_dir is not None:
            summary_writer = tf.summary.create_file_writer(tensorboard_dir)

        print("Start Traning ... ")
        for epoch in range(epochs):
            if streaming is True:
                array_a = np.zeros(shape=(ids_hash_bucket_size,), dtype=np.float32)
                array_b = np.ones(shape=(ids_hash_bucket_size,), dtype=np.float32) * beta
            total_loss = 0.0
            batches_train_time = 0.0
            batches_load_data_time = 0.0
            epoch_trian_time = 0.0
            epoch_load_data_time = 0.0
            step = 1
            
            batch_load_data_start = time.time()
            for inputs in dataset:
                batch_load_data_stop = time.time()
                if streaming is True:
                    cand_ids = inputs[1].get(ids_column)
                    cand_hash_indexs = hash_simple(cand_ids, ids_hash_bucket_size)
                    array_a, array_b, sampling_p = sampling_p_estimation_single_hash(array_a, array_b, cand_hash_indexs, step)
                else:
                    sampling_p = None
                
                batch_train_start = time.time()
                total_loss += distributed_train_step(inputs, sampling_p=sampling_p)
                # total_loss += train_step(inputs, sampling_p=sampling_p)
                batch_train_stop = time.time()
                batch_train_time = batch_train_stop - batch_train_start
                
                batches_train_time += batch_train_time
                epoch_trian_time += batch_train_time

                batch_load_data_time = batch_load_data_stop - batch_load_data_start
                batches_load_data_time += batch_load_data_time
                epoch_load_data_time += batch_load_data_time
                
                if step % 50 == 0:
                    print("Epoch[{}/{}]: Batch({}/{}) "
                            "DataSpeed: {:.4f}sec/batch "
                            "TrainSpeed: {:.4f}sec/batch "
                            "correct_sfx_loss={:.4f} "
                            "topk_recall={:.4f} "
                            "topk_positive={:.4f}".format(
                            epoch+1, epochs, step, steps,
                            batches_load_data_time/50,
                            batches_train_time/50,
                            total_loss/step, 
                            epoch_recall_avg.result(), 
                            epoch_positive_avg.result()))
                    batches_train_time = 0.0
                    batches_load_data_time = 0.0
                step += 1
                batch_load_data_start = time.time()

            # optimizer.lr = 0.1 * optimizer.lr

            loss_results.append(total_loss/steps)
            recall_results.append(epoch_recall_avg.result())
            positive_results.append(epoch_positive_avg.result())
        
            print("Epoch[{}/{}]: correct_sfx_loss={:.4f} topk_recall={:.4f} topk_positive={:.4f}".format(
                    epoch+1, epochs, total_loss/step, epoch_recall_avg.result(), epoch_positive_avg.result()))
            print("Epoch[{}/{}]: Train time: {:.4f}".format(epoch+1, epochs, epoch_trian_time))
            print("Epoch[{}/{}]: Load data time: {:.4f}".format(epoch+1, epochs, epoch_load_data_time))
            
            if tensorboard_dir is not None:
                with summary_writer.as_default(): # pylint: disable=not-context-manager
                    tf.summary.scalar('correct_sfx_loss', total_loss/steps, step=epoch)
                    tf.summary.scalar('topk_recall', epoch_recall_avg.result(), step=epoch)
                    tf.summary.scalar('topk_positive', epoch_positive_avg.result(), step=epoch)

            if (epoch+1) % 2 == 0:
                left_checkpointer.save(left_checkpoint_prefix)
                print(f'Saved checkpoints to: {left_checkpoint_prefix}')
                right_checkpointer.save(right_checkpoint_prefix)
                print(f'Saved checkpoints to: {right_checkpoint_prefix}')

            epoch_recall_avg.reset_states()
            epoch_positive_avg.reset_states()
                
    return left_model, right_model

