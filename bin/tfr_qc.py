#!/usr/bin/env python
from optparse import OptionParser
import glob
import json
import multiprocessing
import os
import pdb
import sys

from natsort import natsorted
import numpy as np
import pandas as pd
import tensorflow as tf
import tqdm

import matplotlib.pyplot as plt
import seaborn as sns

'''
tfr_qc.py

Print quality control statistics for a TFRecords dataset.
'''

################################################################################
# main
################################################################################
def main():
  usage = 'usage: %prog [options] <tfr_data_dir>'
  parser = OptionParser(usage)
  parser.add_option('-o', dest='out_dir',
    default='tfr_qc',
    help='Output directory [Default: %default]')
  parser.add_option('-p', dest='processes',
    default=16, type='int',
    help='Number of parallel threads to use [Default: %default]')
  parser.add_option('-s', dest='split',
    default='test',
    help='TFRecords prefix [Default: %default]')
  (options,args) = parser.parse_args()

  if len(args) != 1:
    parser.error('Must provide TFRecords data directory')
  else:
    tfr_data_dir = args[0]

  if not os.path.isdir(options.out_dir):
    os.mkdir(options.out_dir)

  # read target datasets
  targets_file = '%s/targets.txt' % tfr_data_dir
  targets_df = pd.read_table(targets_file, index_col=0)

  # read data parameters
  stats_file = '%s/statistics.json' % tfr_data_dir
  with open(stats_file) as stats_open:
    data_stats = json.load(stats_open)
  num_targets = data_stats['num_targets']
  target_length = data_stats['target_length']
  num_seqs = data_stats[f'{options.split}_seqs']

  # read target values
  tfr_pattern = '%s/tfrecords/%s*.tfr' % (tfr_data_dir, options.split)
  targets = read_tfr(tfr_pattern, target_length, num_seqs)

  # compute stats
  target_means = np.mean(targets, axis=(0,1), dtype='float64')
  target_max = np.max(targets, axis=(0,1))

  # print statistics for each target
  table_out = open('%s/table.txt' % options.out_dir, 'w')
  for ti in range(targets.shape[2]):
    cols = (ti, target_means[ti], target_max[ti], targets_df.identifier[ti], targets_df.description[ti])
    print('%-4d  %8.3f  %7.3f  %16s  %s' % cols, file=table_out)
  table_out.close()

  # plot distributions for each target
  distr_dir = '%s/distr' % options.out_dir
  if not os.path.isdir(distr_dir):
    os.mkdir(distr_dir)

  # initialize multiprocessing pool
  pool = multiprocessing.Pool(options.processes)

  plot_distr_args = []
  for ti in range(targets.shape[2]):
    targets_ti = np.random.choice(targets[:,:,ti].flatten(), size=10000, replace=False)
    plot_distr_args.append((targets_ti, '%s/t%d.pdf' % (distr_dir,ti)))

  pool.starmap(plot_distr, plot_distr_args)

def plot_distr(targets_ti, out_pdf):
  plt.figure()
  sns.displot(targets_ti)
  plt.savefig(out_pdf)
  plt.close()


def read_tfr(tfr_pattern, target_len, num_seqs):
  tfr_files = natsorted(glob.glob(tfr_pattern))
  if tfr_files:
    dataset = tf.data.Dataset.list_files(tf.constant(tfr_files), shuffle=False)
  else:
    print('Cannot order TFRecords %s' % tfr_pattern, file=sys.stderr)
    dataset = tf.data.Dataset.list_files(tfr_pattern)
  dataset = dataset.flat_map(file_to_records)
  dataset = dataset.map(parse_proto)
  dataset = dataset.batch(1)

  targets = []

  # collect inputs and outputs
  for _, targets_raw in tqdm.tqdm(dataset, total=num_seqs):
    targets1 = targets_raw.numpy().reshape((target_len,-1))
    if np.isnan(targets1).any():
      print('NaN target values')
    elif np.isinf(targets1).any():
      print('Inf target values')

    targets.append(targets1.astype('float16'))

  return np.array(targets)


def file_to_records(filename):
    return tf.data.TFRecordDataset(filename, compression_type='ZLIB')

def parse_proto(example_protos):
  features = {
    'sequence': tf.io.FixedLenFeature([], tf.string),
    'target': tf.io.FixedLenFeature([], tf.string)
  }
  parsed_features = tf.io.parse_example(example_protos, features=features)
  seq = tf.io.decode_raw(parsed_features['sequence'], tf.uint8)
  targets = tf.io.decode_raw(parsed_features['target'], tf.float16)
  return seq, targets


################################################################################
# __main__
################################################################################
if __name__ == '__main__':
  main()
