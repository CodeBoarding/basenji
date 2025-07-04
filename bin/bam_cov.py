#!/usr/bin/env python
# Copyright 2017 Calico LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================
from array import array
from optparse import OptionParser
from collections import OrderedDict
import gc
import h5py
import itertools
import math
import os
import pdb
import random
import sys
import time

import matplotlib.pyplot as plt
import numpy as np
import pyBigWig
import pysam
from scipy.ndimage.filters import gaussian_filter1d
from scipy.optimize import minimize
from scipy.stats import norm, poisson
from scipy.sparse import csr_matrix
import seaborn as sns
from sklearn.linear_model import Ridge
from sklearn.preprocessing import PolynomialFeatures
from sklearn.pipeline import make_pipeline

'''
bam_cov.py

Transform BAM alignments to a normalized BigWig coverage track.

Notes:
 -The adaptive trimming statistics are awry for paired end shift_center datasets
  like ChIP-seq because the events are initially double counted in order to use
  uint16 rather than a larger data structure.
'''

################################################################################
# main
################################################################################
def main():
  usage = 'usage: %prog [options] <bam_file> <output_file>'
  parser = OptionParser(usage)
  parser.add_option('-a', dest='all_overlap',
      default=False, action='store_true',
      help='Events at all overlapping positions [Default: %default]')
  parser.add_option('-b', dest='cut_bias_kmer',
      default=None, action='store_true',
      help='Unfinished! Normalize coverage for a cutting bias model for k-mers [Default: %default]')
  parser.add_option('-c', dest='shift_center',
      default=False, action='store_true',
      help='Shift the event to the fragment center, learning the distribution for single end reads [Default: %default]')
  parser.add_option('--clip_max', dest='clip_max',
      default=None, type='int',
      help='Clip coverage using adaptively-determined thresholds to this maximum [Default: %default]')
  parser.add_option('--clip_multi', dest='clip_max_multi',
      default=None, type='float',
      help='Maximum coverage at a single position from multi-mapping reads [Default: %default]')
  parser.add_option('-f', dest='fasta_file',
      default=None, help='FASTA to obtain sequence to control for GC% [Default: %default]')
  parser.add_option('--sizes', dest='size_file',
      default=None, help='Chromosome sizes (if different subset of chromosomes from alignment step) [Default: %default]')
  parser.add_option('-g', dest='gc',
      default=False, action='store_true',
      help='Control for local GC% [Default: %default]')
  parser.add_option('-m', dest='multi_em',
      default=0, type='int',
      help='EM iterations to distribute multi-mapping reads [Default: %default]')
  parser.add_option('-o', dest='out_dir',
      default='bam_cov', help='Output directory [Default: %default]')
  parser.add_option('-s', '--smooth_multi', dest='smooth_multi_sd',
      default=16, type='float',
      help='Gaussian standard deviation to smooth coverage estimates with [Default: %default]')
  parser.add_option('--smooth_out', dest='smooth_out_sd',
      default=0, type='float',
      help='Gaussian standard deviation to smooth coverage estimates with [Default: %default]')
  parser.add_option('--strand', dest='stranded',
      default=False, action='store_true',
      help='Stranded sequencing, output forward and reverse coverage tracks [Default: %default]')
  parser.add_option('-t', dest='maps_t',
      default=20, type='int',
      help='Multi-mapper threshold [Default: %default]')
  parser.add_option('-u', dest='all_unique',
      default=False, action='store_true',
      help='Treat all alignments as unique [Default: %default]')
  parser.add_option('--unsorted', dest='unsorted',
      default=False, action='store_true',
      help='Alignments are unsorted [Default: %default]')
  parser.add_option('-v', dest='shift_forward_end',
      default=0, type='int',
      help='Fragment shift for forward end read [Default: %default]')
  parser.add_option('-w', dest='shift_reverse_end',
      default=0, type='int',
      help='Fragment shift for reverse end read [Default: %default]')
  (options, args) = parser.parse_args()

  if len(args) != 2:
    parser.error('Must provide input BAM file and output HDF5/BigWig filename')

  else:
    bam_file = args[0]
    output_file = args[1]

  if not os.path.isdir(options.out_dir):
    os.mkdir(options.out_dir)

  ################################################################
  # initialize genome coverage

  # get chromosome lengths (from BAM or from genome sizes file)
  chrom_lengths = None
  if options.size_file is None :
    bam_in = pysam.AlignmentFile(bam_file)
    chrom_lengths = OrderedDict(zip(bam_in.references, bam_in.lengths))
    bam_in.close()
    
  else :
    with open(options.size_file, 'rt') as size_f :
      
      references = []
      lengths = []
      for line in size_f.readlines() :
        line_parts = line.strip().split('\t')
        
        if len(line_parts) == 2 :
          references.append(line_parts[0])
          lengths.append(int(line_parts[1]))
      
      chrom_lengths = OrderedDict(zip(references, lengths))

  # determine single or paired
  sp = single_or_pair(bam_file)

  # initialize
  genome_coverage = GenomeCoverage(
      chrom_lengths,
      stranded=options.stranded,
      track_multi=(options.multi_em > 0),
      smooth_multi_sd=options.smooth_multi_sd,
      clip_max=options.clip_max,
      clip_max_multi=options.clip_max_multi,
      shift_center=options.shift_center,
      shift_forward=options.shift_forward_end,
      shift_reverse=options.shift_reverse_end,
      all_overlap=options.all_overlap,
      all_unique=options.all_unique,
      fasta_file=options.fasta_file,
      maps_t=options.maps_t)

  # estimate fragment shift
  if options.shift_center:
    if sp == 'single':
      genome_coverage.learn_shift_single(bam_file, out_dir=options.out_dir)
    else:
      genome_coverage.learn_shift_pair(bam_file)

  # read alignments
  genome_coverage.read_bam(bam_file, genome_sorted=~options.unsorted)

  ################################################################
  # run EM to distribute multi-mapping read weights

  if options.multi_em > 0:
    genome_coverage.distribute_multi(options.multi_em)

  ################################################################
  # compute k-mer cut bias normalization

  # unfinished.

  # if options.cut_bias_kmer is not None:
  #   kmer_norms = compute_cut_norms(options.cut_bias_kmer, multi_weight_matrix,
  #                                  chromosomes, chrom_lengths,
  #                                  options.fasta_file)

  ################################################################
  # normalize for GC content

  if options.gc:
    genome_coverage.learn_gc(out_dir=options.out_dir)

  ################################################################
  # compute genomic coverage / normalize for cut bias / output

  genome_coverage.write(output_file, sp, smooth_sd=options.smooth_out_sd)


def compute_cut_norms(cut_bias_kmer, read_weights, chromosomes, chrom_lengths, fasta_file):
  """ Compute cut bias normalizations.

        UNFINISHED!

    Args:
     cut_bias_kmer
     read_weights
     chromosomes
     chrom_lengths
     fasta_file

     Returns:
      kmer_norms
    """

  lengths_list = list(chrom_lengths.values())

  kmer_left = cut_bias_kmer // 2
  kmer_right = cut_bias_kmer - kmer_left - 1

  # initialize kmer cut counts
  kmer_cuts = initialize_kmers(cut_bias_kmer, 1)

  # open fasta
  fasta_open = pysam.Fastafile(fasta_file)

  # traverse genome and count
  gi = 0
  for ci in range(len(chromosomes)):
    cl = lengths_list[ci]

    # compute chromosome coverage
    chrom_coverage = np.array(
        read_weights[:, gi:gi + cl].sum(axis=0, dtype='float32')).squeeze()

    # read in the chromosome sequence
    seq = fasta_open.fetch(chromosomes[ci], 0, cl)

    for li in range(kmer_left, cl - kmer_right):
      if chrom_coverage[li] > 0:
        # extract k-mer
        kmer_start = li - kmer_left
        kmer_end = kmer_start + options.cut_bias_kmer
        kmer = seq[kmer_start:kmer_end]

        # consider flipping to reverse complement
        rc_kmer = rc(kmer)
        if kmer > rc(kmer):
          kmer = rc_kmer

        # count
        kmer_cuts[kmer] += genome_coverage[gi]

      gi += 1

  # compute k-mer normalizations
  kmer_sum = sum(kmer_cuts.values())
  kmer_norms = {}
  for kmer in kmer_cuts:
    # kmer_norms[kmer] =
    pass

  return kmer_norms


def row_nzcols_geti(m, ri):
  """ Return row ri's nonzero columns. """
  return m.indices[m.indptr[ri]:m.indptr[ri + 1]]


def row_nzcols_get(m, ri):
  """ Return row ri's nonzero columns. """
  return m.data[m.indptr[ri]:m.indptr[ri + 1]]


def row_nzcols_set(m, ri, v):
  """ Set row ri's nonzero columsn to v. """
  m.data[m.indptr[ri]:m.indptr[ri + 1]] = v


def single_or_pair(bam_file):
  """Check the first read to guess if the BAM has single or paired end reads."""
  bam_in = pysam.AlignmentFile(bam_file)
  align = bam_in.__next__()
  if align.is_paired:
    sp = 'pair'
  else:
    sp = 'single'
  bam_in.close()

  return sp


def distribute_multi_succint(multi_weight_matrix, genome_unique_coverage, multi_window, chrom_lengths, max_iterations=10, converge_t=.01):
  """ Wondering if I can speed things up by vectorizing these operations, but still much to test.

    1. start by comparing the times to make my genome_coverage estimate here with multi_weight_matrix.sum(axis=0) versus looping through the arrays myself.
    2. then benchmark that dot product.
    3. then benchmark the column normalization.
    4. then determine a way to assess convergence. maybe sample 1k reads to act as my proxy?

    """

  print('Distributing %d multi-mapping reads.' % multi_weight_matrix.shape[0])

  lengths_list = list(chrom_lengths.values())

  # initialize genome coverage
  genome_coverage = np.zeros(len(genome_unique_coverage), dtype='float32')

  for it in range(max_iterations):
    print(' Iteration %d' % (it + 1), end='', flush=True)
    t_it = time.time()

    # track convergence
    iteration_change = 0

    # estimate genome coverage
    genome_coverage = genome_unique_coverage + multi_weight_matrix.sum(axis=0)

    # smooth estimate by chromosome
    gi = 0
    for clen in lengths_list:
      genome_coverage[gi:gi + clen] = gaussian_filter1d(
          genome_coverage[gi:gi + clen].astype('float32'), sigma=25, truncate=3)
      gi = gi + clen

    # re-distribute multi-mapping reads
    multi_weight_matrix = multi_weight_matrix.dot(genome_coverage)

    # normalize by column sum
    multi_weight_matrix /= multi_weight_matrix.sum(axis=1)

    # assess coverage (not sure how to do that. maybe an approximation of the big sparse matrix?)
    iteration_change /= multi_weight_matrix.shape[0]
    # print(', %.3f change per multi-read' % iteration_change, flush=True)
    print(
        ' Finished in %ds with %.3f change per multi-read' %
        (time.time() - t_it, iteration_change),
        flush=True)
    if iteration_change / multi_weight_matrix.shape[0] < converge_t:
      break


def initialize_kmers(k, pseudocount=1.):
  """ Initialize a dict mapping kmers to cut counts.

    Args
     k (int): k-mer size
     pseudocount (float): Initial pseudocount

    """
  kmer_cuts = {}
  for i in range(int(math.pow(4, k))):
    kmer = int2kmer(k, i)
    kmer_cuts[kmer] = pseudocount
  return kmer_cuts


def regplot_gc(vals1, vals2, model, out_pdf):
  gold = sns.color_palette('husl', 8)[1]

  plt.figure(figsize=(6, 6))

  # plot data and seaborn model
  ax = sns.regplot(
      x=vals1,
      y=vals2,
      color='black',
      order=3,
      scatter_kws={'color': 'black',
                   's': 4,
                   'alpha': 0.5},
      line_kws={'color': gold})

  # plot my model predictions
  svals1 = np.sort(vals1)
  preds2 = model.predict(svals1[:, np.newaxis])
  ax.plot(svals1, preds2)

  # adjust axis
  ymin, ymax = scatter_lims(vals2)
  ax.set_xlim(0.2, 0.8)
  ax.set_xlabel('GC%')
  ax.set_ylim(ymin, ymax)
  ax.set_ylabel('Coverage')

  ax.grid(True, linestyle=':')

  plt.savefig(out_pdf)
  plt.close()


def regplot_shift(vals1, vals2, preds2, out_pdf):
  gold = sns.color_palette('husl', 8)[1]

  plt.figure(figsize=(6, 6))

  # plot data and seaborn model
  ax = sns.regplot(
      x=vals1,
      y=vals2,
      color='black',
      order=3,
      scatter_kws={'color': 'black',
                   's': 4,
                   'alpha': 0.5},
      line_kws={'color': gold})

  # plot my model predictions
  ax.plot(vals1, preds2)

  # adjust axis
  ymin, ymax = scatter_lims(vals2)
  ax.set_xlabel('Shift')
  ax.set_ylim(ymin, ymax)
  ax.set_ylabel('Covariance')

  ax.grid(True, linestyle=':')

  plt.savefig(out_pdf)
  plt.close()


class GenomeCoverage:
  """
     chrom_lengths (OrderedDict): Mapping chromosome names to lengths.
     fasta (pysam Fastafile):

     unique_counts: G (genomic position) array of unique coverage counts.
     multi_weight_matrix: R (reads) x G (genomic position) array of
     multi-mapping read weight.
     smooth_multi_sd (int): Multi-map distribution gaussian filter standard deviation.
     multi_max (int): Maximum coverage per position from multi-mappers due to
     mis-mapping fear.
     shift (int): Alignment shift to maximize cross-strand coverage correlation
    """

  def __init__(self,
               chrom_lengths,
               stranded=False,
               track_multi=False,
               smooth_multi_sd=1,
               clip_max=None,
               clip_max_multi=None,
               shift_center=False,
               shift_forward=0,
               shift_reverse=0,
               all_overlap=False,
               all_unique=False,
               maps_t=1,
               fasta_file=None):

    # chromosome lookup dictionary
    self.chrom_lookup = OrderedDict()
    for ci, [chrom, _] in enumerate(chrom_lengths.items()):
        self.chrom_lookup[chrom] = ci
    
    self.stranded = stranded
    if self.stranded:
      # model + and - strand of each chromosome
      self.chrom_lengths = OrderedDict()
      for chrom, clen in chrom_lengths.items():
        self.chrom_lengths['%s+'%chrom] = clen
      for chrom, clen in chrom_lengths.items():
        self.chrom_lengths['%s-'%chrom] = clen

    else:
      self.chrom_lengths = chrom_lengths

    self.genome_length = sum(self.chrom_lengths.values())
    self.unique_counts = np.zeros(self.genome_length, dtype='float32')
    self.uc_max = np.finfo(self.unique_counts.dtype).max
    self.active_blocks = None

    self.track_multi = track_multi
    self.smooth_multi_sd = smooth_multi_sd

    self.shift_center = shift_center
    self.shift_forward = shift_forward
    self.shift_reverse = shift_reverse
    self.all_overlap = all_overlap

    self.clip_max = clip_max
    self.clip_max_multi = clip_max_multi

    self.adaptive_cdf = 0.01
    self.adaptive_t = {}

    self.all_unique = all_unique
    self.maps_t = maps_t

    self.fasta = None
    if fasta_file is not None:
      self.fasta = pysam.Fastafile(fasta_file)

    self.gc_model = None


  def align_shifts(self, align):
    """ Helper function to determine alignment event position shifts. """

    if self.shift_center:
      if align.is_proper_pair:
        # shift proper pairs according to mate
        align_shift_forward = abs(align.template_length) // 2
      else:
        # shift others by estimated amount
        align_shift_forward = self.shift_forward

      # match reverse to forward
      align_shift_reverse = align_shift_forward

    else:
      # apply user-specific shifts
      align_shift_forward = self.shift_forward
      align_shift_reverse = self.shift_reverse

    return align_shift_forward, align_shift_reverse


  def distribute_multi(self, max_iterations=4, converge_t=.05):
    """ Distribute multi-mapping read weight proportional to coverage in a local window.

        In
         max_iterations: Maximum iterations through the reads.
         converge_t: Per read weight difference below which we consider
         convergence.

        """
    t0 = time.time()
    num_multi_reads = self.multi_weight_matrix.shape[0]
    print('Distributing %d multi-mapping reads.' % num_multi_reads, flush=True)

    # choose read indexes at which we'll re-estimate genome coverage
    #  (currently, estimate_coverage takes up more time unless the # reads
    #   is huge, but that may change if I can better optimize it.)
    iteration_estimates = max(1, int(np.round(num_multi_reads // 20000000)))
    restimate_indexes = np.linspace(0, num_multi_reads,
                                    iteration_estimates + 1)[:-1]

    # initialize genome coverage
    genome_coverage = np.zeros(len(self.unique_counts), dtype='float32')

    for it in range(max_iterations):
      print(' Iteration %d' % (it + 1), end='', flush=True)
      t_it = time.time()

      # track convergence
      iteration_change = 0

      # re-allocate multi-reads proportionally to coverage estimates
      print('  Re-allocating multi-reads.', end='', flush=True)
      t_r = time.time()
      for ri in range(num_multi_reads):
        # update user
        if ri % 10000000 == 10000000 - 1:
          t_report = time.time() - t_r
          print('\n  processed %d reads in %ds' % (ri+1, t_report),
                end='', flush=True)
          t_r = time.time()

        # update genome coverage estimates
        if ri in restimate_indexes:
          print('  Estimating genomic coverage.', end='', flush=True)
          self.estimate_coverage(genome_coverage)
          print(' Done.', flush=True)

        # get read's aligning positions
        multi_read_positions = row_nzcols_geti(self.multi_weight_matrix, ri)
        multi_positions_len = len(multi_read_positions)

        # dodge disposed reads
        if multi_positions_len > 0:
          # get previous weights
          multi_read_prev = row_nzcols_get(self.multi_weight_matrix, ri)

          # get coverage estimates
          multi_positions_coverage = np.array(
              [genome_coverage[pos] for pos in multi_read_positions])

          # normalize coverage as weights
          mpc_sum = multi_positions_coverage.sum(dtype='float64')
          if mpc_sum > 0:
            multi_positions_weight = multi_positions_coverage / mpc_sum
          else:
            print('Error: read %d coverage sum == %.4f' % (ri, mpc_sum), file=sys.stderr)
            print(multi_positions_coverage, file=sys.stderr)
            exit(1)

          # compute change
          read_pos_change = np.abs(multi_read_prev -
                                   multi_positions_weight).sum()

          if read_pos_change > .001:
            # track convergence
            iteration_change += read_pos_change

            # set new weights
            row_nzcols_set(self.multi_weight_matrix, ri, multi_positions_weight)

      # set new position-specific clip thresholds
      if self.clip_max is not None:
        self.set_clips(genome_coverage)

      # clean up temp storage
      gc.collect()

      # assess coverage
      iteration_change /= self.multi_weight_matrix.shape[0]
      print(' Complete iteration in %ds with %.3f change per multi-read' %
          (time.time() - t_it, iteration_change), flush=True)
      if iteration_change < converge_t:
        break

  def estimate_coverage(self, genome_coverage, pseudocount=.01):
    """ Estimate smoothed genomic coverage.

        In
         genome_coverage: G (genomic position) array of estimated coverage
         counts.
         pseudocount (int): Coverage pseudocount.
        """

    lengths_list = list(self.chrom_lengths.values())

    # start with unique coverage, and add pseudocount
    np.copyto(genome_coverage, self.unique_counts)
    genome_coverage += pseudocount

    # add in multi-map coverage
    for ri in range(self.multi_weight_matrix.shape[0]):
      ri_ptr, ri_ptr1 = self.multi_weight_matrix.indptr[ri:ri + 2]
      ci = 0
      for pos in self.multi_weight_matrix.indices[ri_ptr:ri_ptr1]:
        multi_cov = self.multi_weight_matrix.data[ri_ptr + ci]
        if self.clip_max_multi is not None:
          multi_cov = np.clip(multi_cov, 0, self.clip_max_multi)
        genome_coverage[pos] += multi_cov
        ci += 1

    # limit duplicates
    if self.clip_max is not None:
      self.clip_multi(genome_coverage)
    """ if I switch to active blocks method
        if self.active_blocks is None:
            # determine centromere-like empty blocks
            print('Inferring active blocks.', flush=True)
            self.infer_active_blocks_groupby(genome_coverage)
        """

    # smooth by chromosome
    if self.smooth_multi_sd > 0:
      gi = 0
      for clen in lengths_list:
        gc.collect()
        genome_coverage[gi:gi + clen] = gaussian_filter1d(
            genome_coverage[gi:gi + clen].astype('float32'),
            sigma=self.smooth_multi_sd,
            truncate=3)
        gi += clen
      """ if I switch to active blocks method

            for gis, gie in self.active_blocks:
                genome_coverage[gis:gie] =
                gaussian_filter1d(genome_coverage[gis:gie].astype('float32'),
                sigma=self.smooth_sd, truncate=3)
            """

  def gc_normalize(self, chrom, coverage):
    """ Apply a model to normalize for GC content.

        In
         chrom (str): Chromosome
         coverage ([float]): Coverage array
         pseudocount (int): Coverage pseudocount.

        Out
         model (sklearn object): To control for GC%.
        """

    # trim chromosome strand
    if self.stranded and chrom[-1] in '+-':
      chrom = chrom[:-1]

    # get sequence
    if chrom not in self.fasta.references:
      print('%s not found in FASTA, so GC normalization skipped' % chrom)
      norm_coverage = coverage

    else:
      seq = self.fasta.fetch(chrom)
      assert (len(seq) == len(coverage))

      # compute GC boolean vector
      seq_gc = np.array([nt in 'CG' for nt in seq], dtype='float32')

      # gaussian filter1d
      seq_gc_gauss = gaussian_filter1d(seq_gc, sigma=self.fragment_sd, truncate=3)

      # compute norm quantity
      seq_gc_norm = self.gc_model.predict(seq_gc_gauss[:, np.newaxis])

      # apply it
      norm_coverage = coverage * np.exp(-seq_gc_norm + self.gc_base)

    return norm_coverage

  def learn_gc(self, fragment_sd=64, pseudocount=.01, out_dir=None):
    """ Learn a model to normalize for GC content.

        In
         fragment_sd (int): Gaussian filter standard deviation.
         pseudocount (int): Coverage pseudocount.

        Out
         model (sklearn object): To control for GC%.
        """

    t0 = time.time()
    print('Fitting GC model.', flush=True)

    # save
    self.fragment_sd = fragment_sd

    # helpers
    chroms_list = list(self.chrom_lengths.keys())
    fragment_sd3 = int(np.round(3 * self.fragment_sd))

    # gaussian mask
    gauss_kernel = norm.pdf(
        np.arange(-fragment_sd3, fragment_sd3), loc=0, scale=self.fragment_sd)
    gauss_invsum = 1.0 / gauss_kernel.sum()

    #######################################################
    # construct training data

    # determine multi-mapping positions
    multi_positions = sorted(set(self.multi_weight_matrix.indices))

    # traverse the genome and count multi-mappers in bins
    genome_starts_consider = np.arange(0, self.genome_length, 2 * fragment_sd3)[1:-1]
    genome_starts_multi = np.zeros(len(genome_starts_consider))
    gsi = 0
    mpi = 0
    for gs in genome_starts_consider:
      ge = gs + 2 * fragment_sd3
      while mpi < len(multi_positions) and multi_positions[mpi] < ge:
        genome_starts_multi[gsi] += 1
        mpi += 1
      gsi += 1

    # shuffle to break genome order
    shuffle_indexes = np.arange(len(genome_starts_consider))
    np.random.shuffle(shuffle_indexes)
    genome_starts_consider = genome_starts_consider[shuffle_indexes]
    genome_starts_multi = genome_starts_multi[shuffle_indexes]

    # sort by multi-map positions
    trainable_indexes = np.argsort(genome_starts_multi)

    # track stats
    zero_stop = 20000
    nonzero_stop = 10000
    nsample = 0
    ntier = 0
    last_gsm = 0

    # compute position GC content
    train_pos = []
    train_gc = []
    train_cov = []

    for gsi in trainable_indexes:
      gi = genome_starts_consider[gsi]
      gsm = genome_starts_multi[gsi]

      if gsm > last_gsm:
        print(' GC training sequences: %d with %d multi-map positions.'
            % (ntier, last_gsm), flush=True)
        ntier = 0

      # determine chromosome and position
      ci, pos = self.index_genome(gi)

      # get sequence
      seq_start = pos
      seq_end = pos + 2 * fragment_sd3
      if self.stranded:
        seq_chrom = chroms_list[ci][:-1]
      else:
        seq_chrom = chroms_list[ci]

      # filter for chromosomes for which we have sequence
      if seq_chrom in self.fasta.references:
        seq = self.fasta.fetch(seq_chrom, seq_start, seq_end)

        # filter for clean sequences
        if len(seq) == 2 * fragment_sd3 and seq.find('N') == -1:

          # compute GC%
          seq_gc = np.array([nt in 'CG' for nt in seq], dtype='float32')
          gauss_gc = (seq_gc * gauss_kernel).sum() * gauss_invsum
          train_gc.append(gauss_gc)
          train_pos.append(gi)

          # compute coverage
          seq_cov = self.unique_counts[gi - fragment_sd3:
                                       gi + fragment_sd3] + pseudocount
          if self.clip_max is not None:
            seq_cov = np.clip(seq_cov, 0, 2)
          gauss_cov = (seq_cov * gauss_kernel).sum() * gauss_invsum
          train_cov.append(np.log2(gauss_cov))

          # increment
          nsample += 1
          ntier += 1

      # consider stoppping
      if nsample >= zero_stop or gsm > 0 and nsample >= nonzero_stop:
        break

      # advance multi tracker
      last_gsm = gsm

    print(' GC training sequences: %d with %d multi-map positions.' %
        (ntier, last_gsm), flush=True)

    # convert to arrays
    train_gc = np.array(train_gc)
    train_cov = np.array(train_cov)

    #######################################################
    # fit model

    # polynomial regression
    self.gc_model = make_pipeline(PolynomialFeatures(3), Ridge())
    self.gc_model.fit(train_gc[:, np.newaxis], train_cov)

    # print score
    score = self.gc_model.score(train_gc[:, np.newaxis], train_cov)

    # determine genomic baseline
    self.learn_gc_base()

    print(' Done in %ds.' % (time.time() - t0))
    print('GC model explains %.4f of variance.' % score, flush=True)

    if out_dir is not None:
      # plot training fit
      regplot_gc(train_gc, train_cov, self.gc_model,
                 '%s/gc_model.pdf' % out_dir)

      # print table
      gc_out = open('%s/gc_table.txt' % out_dir, 'w')
      gc_range = np.arange(0, 1, .01)
      gc_norm = self.gc_model.predict(gc_range[:, np.newaxis])
      for i in range(len(gc_range)):
        print(gc_range[i], gc_norm[i], file=gc_out)
      gc_out.close()

  def learn_gc_base(self):
    """ Determine the genome-wide GC model baseline

        In
         self.gc_model

        Out
         self.gc_base
        """

    # helper
    chroms_list = list(self.chrom_lengths.keys())
    fragment_sd3 = int(np.round(3 * self.fragment_sd))

    # gaussian mask
    gauss_kernel = norm.pdf(
        np.arange(-fragment_sd3, fragment_sd3), loc=0, scale=self.fragment_sd)
    gauss_invsum = 1.0 / gauss_kernel.sum()

    # sample genomic positions
    sample_every = self.genome_length / 10000
    train_positions = list(np.arange(0, self.genome_length, sample_every))[1:]
    train_gc = []

    # fetch GC%
    for gi in train_positions:
      # determine chromosome and position
      ci, pos = self.index_genome(gi)

      # get sequence
      seq_start = max(0, pos - fragment_sd3)
      seq_end = pos + fragment_sd3
      if self.stranded:
        seq_chrom = chroms_list[ci][:-1]
      else:
        seq_chrom = chroms_list[ci]

      if seq_chrom in self.fasta.references:
        seq = self.fasta.fetch(seq_chrom, seq_start, seq_end)

        # filter for clean sequences
        if len(seq) == 2 * fragment_sd3 and seq.find('N') == -1:

          # compute GC%
          seq_gc = np.array([nt in 'CG' for nt in seq], dtype='float32')
          gauss_gc = (seq_gc * gauss_kernel).sum() * gauss_invsum
          train_gc.append(gauss_gc)

    # convert to arrays
    train_gc = np.array(train_gc)

    # compute mean prediction
    self.gc_base = np.mean(self.gc_model.predict(train_gc[:, np.newaxis]))

  def index_genome(self, gi):
    """ Compute chromosome and position for a genome index.

        Args
         gi (int): Genomic index

        Returns:
         ci (int): Chromosome index.
         pos (int): Position.
        """

    lengths_list = list(self.chrom_lengths.values())

    # chromosome index
    ci = 0

    # helper counters
    gii = 0
    cii = 0

    # while gi is beyond this chromosome
    while ci < len(lengths_list) and gi - gii > lengths_list[ci]:
      # advance genome index
      gii += lengths_list[ci]

      # advance chromosome
      ci += 1

    # we shouldn't be beyond the chromosomes
    assert (ci < len(lengths_list))

    # set position
    pos = gi - gii

    return ci, pos

  def genome_index(self, ci, pos, strand=None):
    """ Compute genome index for a chromosome index and position.

        Args
         ci (int): Chromosome index.
         pos (int): Position.
         strand (str): '+' or '-'

        Returns:
         gi (int): Genomic index
        """

    lengths_list = list(self.chrom_lengths.values())

    if strand == '-':
      # move the BAM chromosome index to the second half of my list
      ci += len(self.chrom_lengths) // 2

    gi = 0
    for gci in range(len(lengths_list)):
      if ci == gci:
        gi += pos
        break
      else:
        gi += lengths_list[gci]
    return gi

  def genome_indexes(self, ci, positions, strand=None):
    """ Compute genome indexes for a chromosome index and positions.

        Args
         ci (int): Chromosome index.
         positions (int): Position(s)
         strand (str): '+' or '-'

        Returns:
         gi (int): Genomic index
        """

    lengths_list = list(self.chrom_lengths.values())

    if strand == '-':
      # move the BAM chromosome index to the second half of my list
      ci += len(self.chrom_lengths) // 2

    # find chromosome start
    cstart = 0
    for gci in range(len(lengths_list)):
      if ci == gci:
        break
      else:
        cstart += lengths_list[gci]

    gi = np.array([cstart]*len(positions))
    gi += positions

    return gi

  def genome_index_chrom(self, chrom, pos, strand=None):
    """ Compute genome index for a chromosome label and position.

        Args
         chrom (str): Chromosome label.
         pos (int): Position.
         strand (str): '+' or '-'

        Returns:
         gi (int): Genomic index
        """

    if strand:
      chrom += strand

    gi = 0
    for cl_chrom, cl_len in self.chrom_lengths.items():
      if chrom == cl_chrom:
        gi += pos
        break
      else:
        gi += cl_len

    return gi

  def genome_chr(self, genome_indexes, chrom):
    """ Filter and convert an array of genome indexes
            to indexes for a specific chromosome.

        Args
         genome_indexes (np.array):
         chrom (str):

        Returns
         chrom_indexes (np.array)
        """

    chrom_subtract = 0
    for lchrom in self.chrom_lengths:
      if chrom == lchrom:
        break
      else:
        chrom_subtract += self.chrom_lengths[lchrom]

    # filter up to chromosome
    chrom_indexes = genome_indexes[genome_indexes >= chrom_subtract]

    # adjust
    chrom_indexes -= chrom_subtract

    # filter beyond chromosome
    chrom_indexes = chrom_indexes[chrom_indexes < self.chrom_lengths[chrom]]

    return chrom_indexes

  def learn_shift_pair(self, bam_file):
    """ Learn the optimal fragment shift from paired end fragments. """

    t0 = time.time()
    print('Learning shift from paired-end sequences.', end='', flush=True)

    # read proper pair template lengths
    template_lengths = []
    for align in pysam.AlignmentFile(bam_file):
      if align.is_proper_pair and align.is_read1:
        template_lengths.append(abs(align.template_length))

    # compute mean
    self.shift_forward = int(np.round(np.mean(template_lengths) / 2))

    print(' Done in %ds.' % (time.time() - t0))
    print('Shift: %d' % self.shift_forward, flush=True)

  def learn_shift_single(self,
                         bam_file,
                         shift_min=50,
                         shift_max=350,
                         out_dir=None):
    """ Learn the optimal fragment shift that maximizes across strand correlation

             (to be applied for single end discordant alignments.)
    """

    t0 = time.time()
    print('Learning shift from single-end sequences.', end='', flush=True)

    # find the largest chromosome
    chrom_max = None
    for chrom, clen in self.chrom_lengths.items():
      if clen > self.chrom_lengths.get(chrom_max, 0):
        chrom_max = chrom
    chrom_length = self.chrom_lengths[chrom_max]

    # initialize counts
    counts_fwd = np.zeros(chrom_length, dtype='float32')
    counts_rev = np.zeros(chrom_length, dtype='float32')

    # initialize masks
    multi_mask = np.zeros(chrom_length, dtype='bool')

    # compute unique counts for the fwd and rev strands
    for align in pysam.AlignmentFile(bam_file):
      # if aligned to max_chrom
      if not align.is_unmapped and align.reference_name == chrom_max:
        # determine mappability
        multi = (align.has_tag('NH') and align.get_tag('NH') > 1) or align.has_tag('XA')

        if align.is_reverse:
          # determine alignment position
          ci = align.reference_end - 1

          if multi:
            # record as multi-mapping
            multi_mask[ci] = True
          else:
            # count
            counts_rev[ci] += 1

        else:
          # determine alignment position
          ci = align.reference_start - 1

          if multi:
            # record as multi-mapping
            multi_mask[ci] = True
          else:
            # count
            counts_fwd[ci] += 1

    # limit duplicates
    if self.clip_max is not None:
      np.copyto(counts_fwd, np.clip(counts_fwd, 0, 2))
      np.copyto(counts_rev, np.clip(counts_rev, 0, 2))

    # mean normalize
    mean_count = 0.5 * counts_fwd[~multi_mask].mean(
        dtype='float64') + 0.5 * counts_rev[~multi_mask].mean(dtype='float64')
    counts_fwd -= mean_count
    counts_rev -= mean_count

    # compute pearsonr correlations
    telo_buf = chrom_length // 20
    shifts = np.arange(shift_min, shift_max)
    strand_corrs = np.zeros(len(shifts), dtype='float32')
    for si in range(len(shifts)):
      d = shifts[si]
      counts_dot = np.multiply(counts_fwd[telo_buf:-telo_buf],
                               counts_rev[telo_buf + d:-telo_buf + d])
      counts_mask = multi_mask[telo_buf:-telo_buf] | multi_mask[telo_buf + d:
                                                                -telo_buf + d]
      strand_corrs[si] = counts_dot[~counts_mask].mean()

    # polynomial regression
    strand_corrs_smooth = gaussian_filter1d(strand_corrs, sigma=12, truncate=3)

    # find max
    self.shift_forward = (shift_min + np.argmax(strand_corrs_smooth)) // 2

    print(' Done in %ds.' % (time.time() - t0))
    print('Shift: %d' % self.shift_forward, flush=True)

    if out_dir is not None:
      # plot training fit
      regplot_shift(shifts, strand_corrs, strand_corrs_smooth,
                    '%s/shift_model.pdf' % out_dir)

      # print table
      shift_out = open('%s/shift_table.txt' % out_dir, 'w')
      for si in range(len(shifts)):
        print(
            shifts[si],
            strand_corrs[si],
            strand_corrs_smooth[si],
            '*' * int(2 * self.shift_forward == shifts[si]),
            file=shift_out)
      shift_out.close()


  def read_bam(self, bam_file, genome_sorted=False, all_overlap=False):
    """Read alignments from a BAM file into unique and multi data structures."""

    t0 = time.time()
    print('Reading alignments from BAM.', flush=True, end='')

    # intialize sparse matrix in COO format
    multi_reads = array('I')
    multi_positions = array('L')
    multi_weight = array('f')

    # multi-map read index
    ri = 0

    # initialize dict mapping read_id's to indexes
    last_read_id = ''
    if genome_sorted:
      multi_read_index = {}
    
    # initialize dictionary of matches to non-kept chromosome
    chrom_missing_dict = {}

    for align in pysam.AlignmentFile(bam_file):
      if self.all_unique:
        num_maps = 1
      elif align.has_tag('NH'):
        num_maps = align.get_tag('NH')
      elif align.has_tag('XA'):
        num_maps = len(align.get_tag('XA').split(';')[:-1]) + 1
      else:
        num_maps = 1
      
      is_missing_chrom = False
      if not (align.reference_name in self.chrom_lengths or '%s+'%align.reference_name in self.chrom_lengths):
        is_missing_chrom = True
      
      if is_missing_chrom and align.reference_name not in chrom_missing_dict :
        chrom_missing_dict[align.reference_name] = True
        
        print('[Warning] Skipping reads for missing chromosome %s' % align.reference_name, file=sys.stderr)

      if not is_missing_chrom and not align.is_unmapped and num_maps <= self.maps_t and not align.is_duplicate:
        read_id = (align.query_name, align.is_read1)
        
        reference_id = self.chrom_lookup[align.reference_name]

        if self.all_overlap:          
          chrom_pos = np.array(align.get_reference_positions())

        else:
          # set alignment shift          
          align_shift_forward, align_shift_reverse = self.align_shifts(align)

          # determine chromosome length
          if self.stranded:
            chrom_length = self.chrom_lengths['%s+'%align.reference_name]
          else:
            chrom_length = self.chrom_lengths[align.reference_name]

          # set alignment event position
          if align.is_reverse:
            chrom_pos = align.reference_end - 1 - align_shift_reverse
            chrom_pos = max(chrom_pos, 0)
          else:
            chrom_pos = align.reference_start + align_shift_forward
            chrom_pos = min(chrom_pos, chrom_length-1)

          # make singleton list to sync w/ overlaps
          chrom_pos = np.array([chrom_pos])

        skip_faulty_read = False
        # set genome index
        if self.stranded:
          
          #determine strand
          strand = None
          if align.is_paired:
            if align.is_read1 and align.is_reverse and not align.mate_is_reverse:
              strand = '+'
            elif align.is_read1 and not align.is_reverse and align.mate_is_reverse:
              strand = '-'
            elif not align.is_read1 and align.is_reverse and not align.mate_is_reverse:
              strand = '-'
            elif not align.is_read1 and not align.is_reverse and align.mate_is_reverse:
              strand = '+'
          else:
            strand = '+'*(not align.is_reverse) + '-'*(align.is_reverse)
          
          if strand is None:
            skip_faulty_read = True
          else:
            gis = self.genome_indexes(reference_id, chrom_pos, strand)
        else:
          gis = self.genome_indexes(reference_id, chrom_pos)
        
        if not skip_faulty_read:
          assert(np.all(np.greater_equal(gis, 0)))
          assert(np.all(np.less(gis, len(self.unique_counts))))

          # count unique
          if num_maps == 1:
            self.unique_counts[gis] += 1 / len(gis)

          elif not self.track_multi:
            # distribute uniformly between multi-mapping positions
            self.unique_counts[gis] += 1 / len(gis) / num_maps

            # this breaks if the alternative mapping locations aren't independent reads,
            # but are instead stored in the primary alignment tags
            assert(not align.has_tag('XA'))

          else:
            # count BWA multi-mapper
            if align.has_tag('XA') and not align.has_tag('NH'):
              # update multi-map data structures
              ri = self.read_multi_bwa(multi_positions, multi_reads, multi_weight, align, gis[0], ri, align_shift_forward, align_shift_reverse)

            # count NH-tag multi-mapper
            elif align.has_tag('NH') and not align.has_tag('XA'):
            # update multi-map data structures
              ri = self.read_multi_nh(multi_positions, multi_reads, multi_weight, align, gis, ri,
                read_id, last_read_id, genome_sorted, multi_read_index)

            else:
              print('Multi-map tag scenario that I did not prepare for:', file=sys.stderr)
              print(align, file=sys.stderr)
              exit(1)

      if not genome_sorted:
        last_read_id = read_id

    num_multi_reads = ri
    print(' Done in %ds.' % (time.time() - t0), flush=True)

    # convert sparse matrix
    t0 = time.time()
    print('Constructing multi-read CSR matrix.', flush=True, end='')
    self.multi_weight_matrix = csr_matrix(
        (multi_weight, (multi_reads, multi_positions)),
        shape=(num_multi_reads, self.genome_length),
        dtype='float32')
    print(' Done in %ds.' % (time.time() - t0), flush=True)

    # validate that initial weights sum to 1
    # if genome_sorted:
    #   multi_sum = self.multi_weight_matrix.sum(axis=1)
    #   warned = False
    #   disposed_reads = 0
    #   for read_id, ri in multi_read_index.items():
    #     if not np.isclose(multi_sum[ri], 1, rtol=1e-3):
    #       # warn user NH tags don't match
    #       if not warned:
    #         print(
    #             'Multi-weighted coverage for (%s,%s) %f != 1' %
    #             (read_id[0], read_id[1], multi_sum[ri]),
    #             file=sys.stderr)
    #         warned = True

    #       # dispose
    #       row_nzcols_set(self.multi_weight_matrix, ri, 0)
    #       disposed_reads += 1

    #   if disposed_reads > 0:
    #     disposed_pct = disposed_reads / len(multi_sum)
    #     print(
    #         '%d (%.4f) multi-reads were disposed because of incorrect NH sums.' %
    #         (disposed_reads, disposed_pct),
    #         end='',
    #         file=sys.stderr)
    #     if disposed_pct < 0.15:
    #       print(' Proceeding with caution.', file=sys.stderr)
    #     else:
    #       print(' Something is likely awry-- exiting', file=sys.stderr)
    #       exit(1)

    #     # eliminate zeros for disposed reads
    #     self.multi_weight_matrix.eliminate_zeros()


  def infer_active_blocks(self, genome_coverage, min_inactive=50000):
    # compute inactive blocks
    self.active_blocks = []
    active_start = 0
    zero_start = None
    zero_run = 0

    # compute zero booleans
    zero_counts = (genome_coverage == 0)

    i = 0
    for zc in zero_counts:
      # if zero count
      if zc:
        # if it's the first
        if zero_start is None:
          # set the start
          zero_start = i

        # increment the run counter
        zero_run += 1

      # if it's >0
      else:
        # if a sufficiently long zero run is ending
        if zero_run >= min_inactive:
          # unless it's the first nonzero entry
          if active_start < zero_start:
            # save the previous active block
            self.active_blocks.append((active_start, zero_start))

          # begin a new active block
          active_start = i

        # set active_start if it the chr starts without a zero block
        if active_start is None:
          active_start = i

        # refresh zero counters
        zero_start = None
        zero_run = 0

      i += 1

    # consider a final active block
    if zero_start is None or zero_run < min_inactive:
      self.active_blocks.append((active_start, len(genome_coverage)))
    else:
      self.active_blocks.append((active_start, zero_start))

    blocks_out = open('active_blocks.txt', 'w')
    for i in range(len(self.active_blocks)):
      print(
          i,
          self.active_blocks[i][0],
          self.active_blocks[i][1],
          self.active_blocks[i][1] - self.active_blocks[i][0],
          file=blocks_out)
    blocks_out.close()

  def infer_active_blocks_groupby(self, genome_coverage, min_inactive=50000):
    """ Infer active genomic blocks that we'll need to consider.

        The non-groupby version is inefficient. This one should improve it,
         but it's unfinished.
        """
    # compute inactive blocks
    self.active_blocks = []
    active_start = 0
    zero_run = 0

    # compute zero runs
    constant_runs = [(k, sum([True for b in g]))
                     for k, g in itertools.groupby(genome_coverage == 0)]

    # # parse runs
    gi = 0
    for pos_count, run_len in constant_runs:
      if pos_count == 0:
        zero_run = run_len
      else:
        if zero_run > min_inactive:
          # unless it's the first nonzero entry
          if active_start < gi - zero_run:
            # save the previous active block
            self.active_blocks.append((active_start, zero_start))

          # begin a new active block
          active_start = i

  def read_multi_bwa(self, multi_positions, multi_reads, multi_weight, align, gi, ri, align_shift_forward, align_shift_reverse):
    """ Helper function to process a BWA multi-mapper. """

    # update multi alignment matrix
    multi_positions.append(gi)

    # get multi-maps
    multi_align_strings = align.get_tag('XA').split(';')[:-1]
    multi_maps = len(multi_align_strings)+1

    # actual number of multi-mappers with existing chromosomes
    multi_maps_actual = 1
    
    # for each multi-map
    for multi_align_str in multi_align_strings:

      # extract alignment information
      multi_chrom, multi_start, multi_cigar, _ = multi_align_str.split(',')
      multi_strand = multi_start[0]
      
      if multi_chrom in self.chrom_lengths or '%s+'%multi_chrom in self.chrom_lengths:
        
        # determine chromosome length
        if self.stranded:
          multi_chrom_length = self.chrom_lengths['%s+'%multi_chrom]
        else:
          multi_chrom_length = self.chrom_lengths[multi_chrom]

        # determine shifted event position
        #  (are positions 0 or 1-based? SAM is 1-based so that's my best guess)
        if multi_strand == '+':
          multi_pos = int(multi_start[1:])-1 + align_shift_forward
          multi_pos = min(multi_pos, multi_chrom_length-1)
        elif multi_strand == '-':
          multi_pos = int(multi_start[1:])-1 + cigar_len(multi_cigar)-1 - align_shift_reverse
          multi_pos = max(multi_pos, 0)
        else:
          print('Bad assumption of initial +- for BWA multimap position: %s' % multi_start, file=sys.stderr)
          exit(1)

        # determine genome index
        if self.stranded:
          mgi = self.genome_index_chrom(multi_chrom, multi_pos, multi_strand)
        else:
          mgi = self.genome_index_chrom(multi_chrom, multi_pos)

        # update multi alignment matrix
        multi_positions.append(mgi)
        multi_maps_actual += 1

    # finish updating multi alignment matrix for all multi alignments
    multi_reads.extend([ri]*multi_maps_actual)
    multi_weight.extend([np.float32(1./multi_maps)]*multi_maps_actual)

    # update read index
    return ri + 1


  def read_multi_nh(self, multi_positions, multi_reads, multi_weight, align, gis, ri,
    read_id, last_read_id, genome_sorted, multi_read_index):
    """ Helper function to process an NH-tagged multi-mapper. """

    # determine multi-mapping state
    nh_tag = align.get_tag('NH')

    # if new read, update index
    if genome_sorted:
      if read_id not in multi_read_index:
        # map it to an index
        multi_read_index[read_id] = ri
        ri += 1
      ari = multi_read_index[read_id]

    else:
      if read_id != last_read_id:
        ri += 1
      ari = ri

    # store alignment matrix
    read_weight = np.float32(1./(nh_tag*len(gis)))
    multi_reads.extend([ari]*len(gis))
    multi_positions.extend(gis)
    multi_weight.extend([read_weight]*len(gis))

    return ri


  def set_clips(self, coverage):
    """ Hash indexes to clip at various thresholds.

        Must run this before running clip_multi, which will use
        self.multi_clip_indexes. The objective is to estimate
        coverage conservatively w/ clip_max and smoothing before
        asking whether the raw coverage count is compelling.

        In:
         coverage (np.array): Pre-clipped genome coverage.

        Out:
          self.adaptive_t (int->float): Clip values mapped to coverage thresholds
                                        above which to apply them.
          self.multi_clip_indexes (int->np.array): Clip values mapped to genomic
                                                   indexes to clip.
        """

    # choose clip thresholds
    if len(self.adaptive_t) == 0:
      for clip_value in range(2, self.clip_max + 1):
        # aiming for .01 cumulative density above the threshold.
        #  decreasing the density increases the thresholds.
        cdf_matcher = lambda u: (self.adaptive_cdf - (1-poisson.cdf(clip_value, u)))**2
        self.adaptive_t[clip_value] = minimize(cdf_matcher, clip_value)['x'][0]

    # take indexes with coverage between this clip threshold and the next
    self.multi_clip_indexes = {}
    for clip_value in range(2, self.clip_max):
      mci = np.where((coverage > self.adaptive_t[clip_value]) &
                     (coverage <= self.adaptive_t[clip_value + 1]))[0]
      if len(mci) > 0:
        self.multi_clip_indexes[clip_value] = mci
      print('Sites clipped to %d: %d' % (clip_value, len(mci)))

    # set the last clip_value
    mci = np.where(coverage > self.adaptive_t[self.clip_max])[0]
    if len(mci) > 0:
      self.multi_clip_indexes[self.clip_max] = mci
    print('Sites clipped to %d: %d' % (self.clip_max, len(mci)))


  def clip_multi(self, coverage, chrom=None):
    """ Clip coverage at adaptively-determined thresholds.

        Must run set_clips to set self.multi_clip_indexes. If self.adaptive_t
        is empty, I know it hasn't been run yet.

        In:
         coverage (np.array): Pre-clipped genome/chromosome coverage.
         chrom (str): Single chromosome coverage.

        Out:
         coverage (np.array): Clipped coverage values
        """

    if len(self.adaptive_t) == 0:
      # we haven't chosen clip thresholds yet. be conservative.
      coverage = np.clip(coverage, 0, self.clip_max)

    else:
      # track clipped indexes
      clipped_indexes = np.zeros(len(coverage), dtype='bool')

      # clip indexes at each value
      for clip_value, clip_indexes in self.multi_clip_indexes.items():

        # adjust for single chromosome
        if chrom is not None:
          clip_indexes = self.genome_chr(clip_indexes, chrom)

        # clip these indexes at this clip_value
        coverage[clip_indexes] = np.clip(coverage[clip_indexes], 0, clip_value)

        # remember we clipped these indexes
        clipped_indexes[clip_indexes] = True

      # clip the remainder to 1
      coverage[~clipped_indexes] = np.clip(coverage[~clipped_indexes], 0, 1)


  def write(self, output_file, single_or_pair, smooth_sd=0, zero_eps=.003):
    """ Compute and write out coverage to bigwig file.

        Go chromosome by chromosome here to facilitate printing,
        and save memory.

        In:
         output_file (str): HDF5 or BigWig filename.
         single_or_pair (bool): Specifies whether to correct for paired end double coverage.
        """

    # choose bigwig or h5
    if os.path.splitext(output_file)[1] == '.bw':
      bigwig = True
      if self.stranded:
        headers = [(chrom[:-1],clen) for chrom,clen in self.chrom_lengths.items() if chrom[-1]=='+']

        # forward
        fcov_out = pyBigWig.open('%s+.bw' % os.path.splitext(output_file)[0], 'w')
        fcov_out.addHeader(headers)

        # reverse
        rcov_out = pyBigWig.open('%s-.bw' % os.path.splitext(output_file)[0], 'w')
        rcov_out.addHeader(headers)

      else:
        cov_out = pyBigWig.open(output_file, 'w')
        cov_out.addHeader(list(self.chrom_lengths.items()))

    else:
      bigwig = False
      if self.stranded:
        fcov_out = h5py.File('%s+.w5' % os.path.splitext(output_file)[0], 'w')
        rcov_out = h5py.File('%s-.w5' % os.path.splitext(output_file)[0], 'w')
      else:
        cov_out = h5py.File(output_file, 'w')

    chroms_list = list(self.chrom_lengths.keys())
    lengths_list = list(self.chrom_lengths.values())

    gi = 0
    for ci in range(len(chroms_list)):
      cl = lengths_list[ci]

      # compute multi-mapper chromosome coverage
      chrom_weight_matrix = self.multi_weight_matrix[:,gi:gi+cl]
      chrom_multi_coverage = np.array(
          chrom_weight_matrix.sum(axis=0, dtype='float32')).squeeze()

      # sum with unique coverage
      chrom_coverage_array = self.unique_counts[gi:gi+cl] + chrom_multi_coverage

      # limit duplicates
      if self.clip_max is not None:
        self.clip_multi(chrom_coverage_array, chroms_list[ci])

      # Gaussian smooth
      if smooth_sd > 0:
        chrom_coverage_array = gaussian_filter1d(
            chrom_coverage_array, sigma=smooth_sd, truncate=3)

      # normalize for GC content
      if self.gc_model is not None:
        pre_sum = chrom_coverage_array.sum()
        chrom_coverage_array = self.gc_normalize(chroms_list[ci],
                                                 chrom_coverage_array)
        post_sum = chrom_coverage_array.sum()
        # print('  GC normalization altered mean coverage by %.3f (%d/%d)' % (post_sum/pre_sum, post_sum, pre_sum))

      # correct for double coverage in paired end data with a single center event
      if single_or_pair == 'pair' and self.shift_center:
        chrom_coverage_array /= 2.0

      # set small values to zero
      chrom_coverage_array[chrom_coverage_array < zero_eps] = 0

      # clip very large values
      chrom_coverage_array = np.clip(chrom_coverage_array,
                                     np.finfo(np.float16).min,
                                     np.finfo(np.float16).max)

      # for stranded output
      if self.stranded:
        # adjust chromosome name
        chrom = chroms_list[ci][:-1]
        strand = chroms_list[ci][-1]

        # choose output file
        if strand == '+':
          cov_out = fcov_out
        else:
          cov_out = rcov_out
      else:
        chrom = chroms_list[ci]

      # add to bigwig
      if bigwig:
        cov_out.addEntries(
            chrom,
            0,
            values=chrom_coverage_array.astype('float16'),
            span=1,
            step=1)
      else:
        cov_out.create_dataset(
            chrom,
            data=chrom_coverage_array,
            dtype='float16',
            compression='gzip',
            shuffle=True)

      # update genomic index
      gi += cl

      # clean up temp storage
      gc.collect()

    cov_out.close()


def cigar_len(cigar_str):
    clen = 0

    cigar_iter = itertools.groupby(cigar_str, lambda c: c.isdigit())
    for g, n_chars in cigar_iter:
        n = int(''.join(n_chars))
        op = ''.join(next(cigar_iter)[1])
        if op in 'MDN=X':
            clen += n

    return clen


def scatter_lims(vals1, vals2=None, buffer=.05):
  if vals2 is not None:
    vals = np.concatenate((vals1, vals2))
  else:
    vals = vals1
  vmin = np.nanmin(vals)
  vmax = np.nanmax(vals)

  buf = .05 * (vmax - vmin)

  if vmin == 0:
    vmin -= buf / 2
  else:
    vmin -= buf
  vmax += buf

  return vmin, vmax

################################################################################
# __main__
################################################################################
if __name__ == '__main__':
  main()
