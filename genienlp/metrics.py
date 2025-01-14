#
# Copyright (c) 2018, Salesforce, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import collections
import os
import re
import string
from argparse import Namespace
from contextlib import closing
from multiprocessing import Pool, cpu_count
from subprocess import PIPE, Popen
from typing import Iterable

import numpy as np
import sacrebleu
from datasets import load_metric
from pyrouge import Rouge155
from seqeval import metrics as seq_metrics
from seqeval import scheme as seq_scheme

from .tasks.generic_dataset import Query
from .util import requote_program


def to_lf(s, table):
    aggs = [y.lower() for y in Query.agg_ops]
    agg_to_idx = {x: i for i, x in enumerate(aggs)}
    conditionals = [y.lower() for y in Query.cond_ops]
    headers_unsorted = [(y.lower(), i) for i, y in enumerate(table['header'])]
    headers = [(y.lower(), i) for i, y in enumerate(table['header'])]
    headers.sort(reverse=True, key=lambda x: len(x[0]))
    condition_s, conds = None, []
    if 'where' in s:
        s, condition_s = s.split('where', 1)

    s = ' '.join(s.split()[1:-2])
    sel, agg = None, 0
    for col, idx in headers:
        if col == s:
            sel = idx
    if sel is None:
        s = s.split()
        agg = agg_to_idx[s[0]]
        s = ' '.join(s[1:])
        for col, idx in headers:
            if col == s:
                sel = idx

    full_conditions = []
    if condition_s is not None:

        condition_s = ' ' + condition_s + ' '
        for idx, col in enumerate(headers):
            condition_s = condition_s.replace(' ' + col[0] + ' ', ' Col{} '.format(col[1]))
        condition_s = condition_s.strip()

        for idx, col in enumerate(conditionals):
            new_s = []
            for t in condition_s.split():
                if t == col:
                    new_s.append('Cond{}'.format(idx))
                else:
                    new_s.append(t)
            condition_s = ' '.join(new_s)
        s = condition_s
        conds = re.split('(Col\d+ Cond\d+)', s)
        if len(conds) == 0:
            conds = [s]
        conds = [x for x in conds if len(x.strip()) > 0]
        full_conditions = []
        for i, x in enumerate(conds):
            if i % 2 == 0:
                x = x.split()
                col_num = int(x[0].replace('Col', ''))
                opp_num = int(x[1].replace('Cond', ''))
                full_conditions.append([col_num, opp_num])
            else:
                x = x.split()
                if x[-1] == 'and':
                    x = x[:-1]
                x = ' '.join(x)
                if 'Col' in x:
                    new_x = []
                    for t in x.split():
                        if 'Col' in t:
                            idx = int(t.replace('Col', ''))
                            t = headers_unsorted[idx][0]
                        new_x.append(t)
                    x = new_x
                    x = ' '.join(x)
                if 'Cond' in x:
                    new_x = []
                    for t in x.split():
                        if 'Cond' in t:
                            idx = int(t.replace('Cond', ''))
                            t = conditionals[idx]
                        new_x.append(t)
                    x = new_x
                    x = ' '.join(x)
                full_conditions[-1].append(x)
    logical_form = {'sel': sel, 'conds': full_conditions, 'agg': agg}
    return logical_form


def computeLFEM(greedy, answer):
    answer = [x[0] for x in answer]
    count = 0
    correct = 0
    text_answers = []
    for idx, (g, ex) in enumerate(zip(greedy, answer)):
        count += 1
        text_answers.append([ex['answer'].lower()])
        try:
            lf = to_lf(g, ex['table'])
            gt = ex['sql']
            conds = gt['conds']
            lower_conds = []
            for c in conds:
                lc = c
                lc[2] = str(lc[2]).lower()
                lower_conds.append(lc)
            gt['conds'] = lower_conds
            correct += lf == gt
        except BaseException:
            continue
    return correct / count * 100, text_answers


def score(answer, gold):
    if len(gold) > 0:
        gold = set.union(*[simplify(g) for g in gold])
    answer = simplify(answer)
    tp, tn, sys_pos, real_pos = 0, 0, 0, 0
    if answer == gold:
        if not ('unanswerable' in gold and len(gold) == 1):
            tp += 1
        else:
            tn += 1
    if not ('unanswerable' in answer and len(answer) == 1):
        sys_pos += 1
    if not ('unanswerable' in gold and len(gold) == 1):
        real_pos += 1
    return np.array([tp, tn, sys_pos, real_pos])


def simplify(answer):
    simplified = answer.strip().lower().split()
    simplified = (''.join(c for c in t if c not in string.punctuation) for t in simplified)
    return set(simplified) - {'the', 'a', 'an', 'and', ''}


# http://nlp.cs.washington.edu/zeroshot/evaluate.py
def computeCF1(greedy, answer):
    scores = np.zeros(4)
    for g, a in zip(greedy, answer):
        scores += score(g, a)
    tp, tn, sys_pos, real_pos = scores.tolist()
    if tp == 0:
        p = r = f = 0.0
    else:
        p = tp / float(sys_pos)
        r = tp / float(real_pos)
        f = 2 * p * r / (p + r)

    return f * 100, p * 100, r * 100


def normalize_text(s):
    """Lower text and remove punctuation, articles and extra whitespace."""

    def remove_articles(text):
        return re.sub(r'\b(a|an|the)\b', ' ', text)

    def white_space_fix(text):
        return ' '.join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return ''.join(ch for ch in text if ch not in exclude)

    def lower(text):
        return text.lower()

    return white_space_fix(remove_articles(remove_punc(lower(s))))


def f1_score(prediction, ground_truth):
    prediction_tokens = prediction.split()
    ground_truth_tokens = ground_truth.split()
    common = collections.Counter(prediction_tokens) & collections.Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0
    precision = 1.0 * num_same / len(prediction_tokens)
    recall = 1.0 * num_same / len(ground_truth_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1


def exact_match(prediction, ground_truth):
    return prediction == ground_truth


def partial_exact_match(prediction, ground_truth):
    prediction = prediction.split()
    ground_truth = ground_truth.split()
    is_correct_token = [p == g for p, g in zip(prediction, ground_truth)]
    partial_score = sum(is_correct_token) / len(is_correct_token)
    return partial_score


def structure_match(prediction, ground_truth):
    return requote_program(prediction) == requote_program(ground_truth)


def metric_max_over_ground_truths(metric_fn, prediction, ground_truths):
    scores_for_ground_truths = []
    for idx, ground_truth in enumerate(ground_truths):
        score = metric_fn(prediction, ground_truth)
        scores_for_ground_truths.append(score)
    return max(scores_for_ground_truths)


def computeSequenceClassificationPrecision(outputs, targets):
    targets = [target[0] for target in targets]
    precision_metric = load_metric('precision')
    return precision_metric.compute(references=targets, predictions=outputs)['precision']


def computeSequenceClassificationRecall(outputs, targets):
    targets = [target[0] for target in targets]
    recall_metric = load_metric('recall')
    return recall_metric.compute(references=targets, predictions=outputs)['recall']


def computeSequenceClassificationF1(outputs, targets):
    targets = [target[0] for target in targets]
    f1_metric = load_metric('f1')
    return f1_metric.compute(references=targets, predictions=outputs)['f1']


def computeF1(outputs, targets):
    outs = [metric_max_over_ground_truths(f1_score, o, t) for o, t in zip(outputs, targets)]
    return sum(outs) / len(outputs) * 100


def computeEM(outputs, targets):
    outs = [metric_max_over_ground_truths(exact_match, o, t) for o, t in zip(outputs, targets)]
    return sum(outs) / len(outputs) * 100


def computePartialEM(outputs, targets):
    outs = [metric_max_over_ground_truths(partial_exact_match, o, t) for o, t in zip(outputs, targets)]
    return sum(outs) / len(outputs) * 100


def computeSM(outputs, targets):
    outs = [metric_max_over_ground_truths(structure_match, o, t) for o, t in zip(outputs, targets)]
    return sum(outs) / len(outputs) * 100


def computeBERTScore(outputs, targets, lang):
    bertscore_metric = load_metric("bertscore")
    return sum(bertscore_metric.compute(predictions=outputs, references=targets, lang=lang)['f1']) / len(outputs) * 100


def computeTER(outputs, targets):
    targets = [[t[i] for t in targets] for i in range(len(targets[0]))]
    args = Namespace(tokenize=sacrebleu.DEFAULT_TOKENIZER)
    ter_metric = sacrebleu.metrics.TER(args)
    return ter_metric.corpus_score(outputs, targets).score * 100


def computeBLEU(outputs, targets):
    targets = [[t[i] for t in targets] for i in range(len(targets[0]))]
    return sacrebleu.corpus_bleu(outputs, targets, lowercase=True).score


def computeCasedBLEU(outputs, targets):
    # lowercase is false
    sacrebleu_metric = load_metric("sacrebleu")
    return sacrebleu_metric.compute(predictions=outputs, references=targets, lowercase=False)['score']


def computeT5BLEU(outputs, targets):
    # tokenize_v14_international is used instead of default tokenize_13a tokenizer
    targets = [[t[i] for t in targets] for i in range(len(targets[0]))]
    return sacrebleu.corpus_bleu(
        outputs,
        targets,
        smooth_method="exp",  # default
        smooth_value=0.0,  # default
        force=False,  # default
        lowercase=False,  # default
        tokenize="intl",
        use_effective_order=False,  # default
    ).score


def computeNMTBLEU(outputs, targets):
    # input should be tokenized
    # TODO figure better tokenization esp. for CJK langs

    outputs = [o.split(" ") for o in outputs]
    targets = [[t.split(" ") for t in values] for values in targets]
    bleu_metric = load_metric("bleu")
    return bleu_metric.compute(predictions=outputs, references=targets)['bleu'] * 100


class Rouge(Rouge155):
    """Rouge calculator class with custom command-line options."""

    # See full list of options here:
    # https://github.com/andersjo/pyrouge/blob/master/tools/ROUGE-1.5.5/README.txt#L82
    DEFAULT_OPTIONS = [
        '-a',  # evaluate all systems
        '-n',
        4,  # max-ngram
        '-x',  # do not calculate ROUGE-L
        '-2',
        4,  # max-gap-length
        '-u',  # include unigram in skip-bigram
        '-c',
        95,  # confidence interval
        '-r',
        1000,  # number-of-samples (for resampling)
        '-f',
        'A',  # scoring formula
        '-p',
        0.5,  # 0 <= alpha <=1
        '-t',
        0,  # count by token instead of sentence
        '-d',  # print per evaluation scores
    ]

    def __init__(self, n_words=None, keep_files=False, options=None):

        if options is None:
            self.options = self.DEFAULT_OPTIONS.copy()
        else:
            self.options = options

        if n_words:
            options.extend(["-l", n_words])

        stem = "-m" in self.options

        super(Rouge, self).__init__(n_words=n_words, stem=stem, keep_files=keep_files)

    def _run_rouge(self):
        # Get full options
        options = ['-e', self._rouge_data] + list(map(str, self.options)) + [os.path.join(self._config_dir, "settings.xml")]

        # logging.info("Running ROUGE with options {}".format(" ".join(options)))
        # print([self._rouge_bin] + list(options))
        pipes = Popen([self._rouge_bin] + options, stdout=PIPE, stderr=PIPE)
        std_out, std_err = pipes.communicate()

        div_by_zero_error = std_err.decode("utf-8").startswith("Illegal division by zero")
        if pipes.returncode == 0 or div_by_zero_error:
            # Still returns the correct output even with div by zero
            return std_out
        else:
            raise ValueError(std_out.decode("utf-8") + "\n" + std_err.decode("utf-8"))


def computeROUGE(greedy, answer):
    rouges = compute_rouge_scores(greedy, answer)
    if len(rouges) > 0:
        avg_rouges = {}
        for key in rouges[0].keys():
            avg_rouges[key] = sum([r.get(key, 0.0) for r in rouges]) / len(rouges) * 100
    else:
        avg_rouges = None
    return avg_rouges


def split_sentences(txt, splitchar=".", include_splitchar=False):
    """Split sentences of a text based on a given EOS char."""
    out = [s.split() for s in txt.strip().split(splitchar) if len(s) > 0]
    return out


def compute_rouge_scores(summs, refs, splitchar='.', options=None, parallel=True):
    assert len(summs) == len(refs)
    options = [
        '-a',  # evaluate all systems
        '-c',
        95,  # confidence interval
        '-m',  # use Porter stemmer
        '-n',
        2,  # max-ngram
        '-w',
        1.3,  # weight (weighting factor for WLCS)
    ]
    rr = Rouge(options=options)
    rouge_args = []
    for summ, ref in zip(summs, refs):
        letter = "A"
        ref_dict = {}
        for r in ref:
            ref_dict[letter] = [x for x in split_sentences(r, splitchar) if len(x) > 0]
            letter = chr(ord(letter) + 1)
        s = [x for x in split_sentences(summ, splitchar) if len(x) > 0]
        rouge_args.append((s, ref_dict))
    if parallel:
        with closing(Pool(cpu_count() // 2)) as pool:
            rouge_scores = pool.starmap(rr.score_summary, rouge_args)
    else:
        rouge_scores = []
        for s, a in rouge_args:
            rouge_scores.append(rr.score_summary(s, ref_dict))
    return rouge_scores


def to_delta_state(line):
    delta_state = {'inform': {}, 'request': {}}
    try:
        if line == 'None' or line.strip() == '' or line.strip() == ';':
            return delta_state
        inform, request = [[y.strip() for y in x.strip().split(',')] for x in line.split(';')]
        inform_pairs = {}
        for i in inform:
            try:
                k, v = i.split(':')
                inform_pairs[k.strip()] = v.strip()
            except BaseException:
                pass
        delta_state = {'inform': inform_pairs, 'request': request}
    except BaseException:
        pass
    finally:
        return delta_state


def update_state(state, delta):
    for act, slot in delta.items():
        state[act] = slot
    return state


def dict_cmp(d1, d2):
    def cmp(a, b):
        for k1, v1 in a.items():
            if k1 not in b:
                return False
            else:
                if v1 != b[k1]:
                    return False
        return True

    return cmp(d1, d2) and cmp(d2, d1)


def computeDialogue(greedy, answer):
    examples = []
    for idx, (g, a) in enumerate(zip(greedy, answer)):
        examples.append((a[0][0], g, a[0][1], idx))
    examples.sort()
    turn_request_positives = 0
    turn_goal_positives = 0
    joint_goal_positives = 0
    ldt = None
    for ex in examples:
        if ldt is None or ldt.split('_')[:-1] != ex[0].split('_')[:-1]:
            state, answer_state = {}, {}
            ldt = ex[0]
        delta_state = to_delta_state(ex[1])
        answer_delta_state = to_delta_state(ex[2])
        state = update_state(state, delta_state['inform'])
        answer_state = update_state(answer_state, answer_delta_state['inform'])
        if dict_cmp(state, answer_state):
            joint_goal_positives += 1
        if delta_state['request'] == answer_delta_state['request']:
            turn_request_positives += 1
        if dict_cmp(delta_state['inform'], answer_delta_state['inform']):
            turn_goal_positives += 1

    joint_goal_em = joint_goal_positives / len(examples) * 100
    turn_request_em = turn_request_positives / len(examples) * 100
    turn_goal_em = turn_goal_positives / len(examples) * 100
    answer = [(x[-1], x[-2]) for x in examples]
    answer.sort()
    answer = [[x[1]] for x in answer]
    return joint_goal_em, turn_request_em, turn_goal_em, answer


def compute_metrics(greedy, answer, requested_metrics: Iterable, lang):
    """
    Inputs:
        requested_metrics: contains a subset of the following metrics
            em (exact match)
            sm (structure match): valid if the output is ThingTalk code. Whether the gold answer and prediction are identical if we ignore parameter values of ThingTalk programs
            bleu
            rouge1, rouge2, rougeL, avg_rouge
            f1: token-level F1 score, tokenizes with whitespace
            nf1: normalize outputs then calculate token-level F1 score
            nem: normalize outputs then calculate exact match
            corpus_f1, precision, recall: corpus-level precision, recall and F1 score
            lfem
            joint_goal_em, turn_request_em, turn_goal_em, avg_dialogue
    """
    metric_keys = []
    metric_values = []
    if not isinstance(answer[0], list):
        answer = [[a] for a in answer]
    if 'lfem' in requested_metrics:
        lfem, answer = computeLFEM(greedy, answer)
        metric_keys += ['lfem']
        metric_values += [lfem]
    if 'joint_goal_em' in requested_metrics:
        joint_goal_em, request_em, turn_goal_em, answer = computeDialogue(greedy, answer)
        avg_dialogue = (joint_goal_em + request_em) / 2
        metric_keys += ['joint_goal_em', 'turn_request_em', 'turn_goal_em', 'avg_dialogue']
        metric_values += [joint_goal_em, request_em, turn_goal_em, avg_dialogue]
    em = computeEM(greedy, answer)
    metric_keys += ['em']
    metric_values += [em]
    if 'pem' in requested_metrics:
        pem = computePartialEM(greedy, answer)
        metric_keys.append('pem')
        metric_values.append(pem)
    if 'sm' in requested_metrics:
        sm = computeSM(greedy, answer)
        metric_keys.append('sm')
        metric_values.append(sm)
    if 'ter' in requested_metrics:
        ter = computeTER(greedy, answer)
        metric_keys.append('ter')
        metric_values.append(ter)
    if 'bertscore' in requested_metrics:
        bertscore = computeBERTScore(greedy, answer, lang)
        metric_keys.append('bertscore')
        metric_values.append(bertscore)
    if 'casedbleu' in requested_metrics:
        casedbleu = computeCasedBLEU(greedy, answer)
        metric_keys.append('casedbleu')
        metric_values.append(casedbleu)
    if 'bleu' in requested_metrics:
        bleu = computeBLEU(greedy, answer)
        metric_keys.append('bleu')
        metric_values.append(bleu)
    if 't5_bleu' in requested_metrics:
        t5_bleu = computeT5BLEU(greedy, answer)
        metric_keys.append('t5_bleu')
        metric_values.append(t5_bleu)
    if 'nmt_bleu' in requested_metrics:
        nmt_bleu = computeNMTBLEU(greedy, answer)
        metric_keys.append('nmt_bleu')
        metric_values.append(nmt_bleu)
    if 'avg_rouge' in requested_metrics:
        rouge = computeROUGE(greedy, answer)
        metric_keys += ['rouge1', 'rouge2', 'rougeL', 'avg_rouge']
        avg_rouge = (rouge['rouge_1_f_score'] + rouge['rouge_2_f_score'] + rouge['rouge_l_f_score']) / 3
        metric_values += [rouge['rouge_1_f_score'], rouge['rouge_2_f_score'], rouge['rouge_l_f_score'], avg_rouge]
    if 'sc_precision' in requested_metrics:
        precision = computeSequenceClassificationPrecision(greedy, answer)
        metric_keys.append('sc_precision')
        metric_values.append(precision)
    if 'sc_recall' in requested_metrics:
        recall = computeSequenceClassificationRecall(greedy, answer)
        metric_keys.append('sc_recall')
        metric_values.append(recall)
    if 'sc_f1' in requested_metrics:
        f1 = computeSequenceClassificationF1(greedy, answer)
        metric_keys.append('sc_f1')
        metric_values.append(f1)
    if 'f1' in requested_metrics:
        f1 = computeF1(greedy, answer)
        metric_keys.append('f1')
        metric_values.append(f1)

    if 'ner_f1_IOB1' in requested_metrics:
        greedy_processed = [pred.split() for pred in greedy]
        answer_processed = [ans[0].split() for ans in answer]

        def convert_IOB2_to_IOB1(labels):
            cur_category = None
            for n, label in enumerate(labels):
                if label[0] == "B" and label[2:] != cur_category:
                    labels[n] = "I" + label[1:]
                cur_category = label[2:]

        convert_IOB2_to_IOB1(greedy_processed)
        convert_IOB2_to_IOB1(answer_processed)
        f1 = (
            seq_metrics.f1_score(y_pred=greedy_processed, y_true=answer_processed, mode='strict', scheme=seq_scheme.IOB1) * 100
        )

        metric_keys.append('ner_f1_IOB1')
        metric_values.append(f1)

    if 'ner_f1' in requested_metrics:
        greedy_processed = [pred.split() for pred in greedy]
        answer_processed = [ans[0].split() for ans in answer]

        f1 = seq_metrics.f1_score(y_pred=greedy_processed, y_true=answer_processed) * 100

        metric_keys.append('ner_f1')
        metric_values.append(f1)

    norm_greedy = [normalize_text(g) for g in greedy]
    norm_answer = [[normalize_text(a) for a in al] for al in answer]
    if 'nf1' in requested_metrics:
        nf1 = computeF1(norm_greedy, norm_answer)
        metric_keys.append('nf1')
        metric_values.append(nf1)
    if 'nem' in requested_metrics:
        nem = computeEM(norm_greedy, norm_answer)
        metric_keys.append('nem')
        metric_values.append(nem)

    if 'corpus_f1' in requested_metrics:
        corpus_f1, precision, recall = computeCF1(norm_greedy, norm_answer)
        metric_keys += ['corpus_f1', 'precision', 'recall']
        metric_values += [corpus_f1, precision, recall]

    metric_dict = dict(zip(metric_keys, metric_values))
    metric_dict = collections.OrderedDict((key, metric_dict[key]) for key in requested_metrics)
    return metric_dict, answer
