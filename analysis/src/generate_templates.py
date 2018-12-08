from collections import Counter
import ftfy
import numpy as np
import pandas as pd
import re
from string import Template

from .data_utils import create_inverted_news_dict, get_teams


def text_normalization(text):
    number_word_dict = {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6', 'seven': '7',
        'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13', 'fourteen': '14',
        'fifteen': '15', 'sixteen': '16', 'seventeen': '17', 'eighteen': '18', 'nineteen': '19', 'twenty': '20',
        'thirty': '30', 'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70', 'eighty': '80', 'ninety': '90'
    }

    def number_words_repl(match):
        return number_word_dict[match.group(0)]

    text = ftfy.fix_text(text)
    text = text.replace('-of-', ' of ')
    text = re.sub(r'\bWeek\b', 'week', text)
    text = re.sub(r'\b(' + '|'.join([k for k in number_word_dict.keys()]) + r')\b',
                  number_words_repl, text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+)-([A-Za-z]+)', r'\1 \2', text)
    text = re.sub(r'(\[A-Za-z]+)-(\d+)', r'\1 \2', text)
    return text


def get_token_ngram_tag(token):
    """Get correct value for ngram models. template tag / text / pos"""
    if token._.template_tag is not None:
        if 'temp_var_' not in token._.template_tag:
            return token._.template_tag
        elif token.pos_ == 'NUM':
            return token.pos_
        else:
            return token.text
    else:
        return token.text


def get_context(token, context_size, by_side=True):
    """Get ngrams in a given context window size left and right of token"""
    left_context, right_context = [], []
    for idx in range(1, context_size + 1):
        try:
            left_context.append(get_token_ngram_tag(token.nbor(-idx)))
        except IndexError:
            pass

        try:
            right_context.append(get_token_ngram_tag(token.nbor(idx)))
        except IndexError:
            pass

    if by_side is True:
        return left_context, right_context
    else:
        return left_context + right_context


def get_context_tags(token):
    """Get the tags to the left and right of token"""
    left_context = None
    right_context = None

    for t in reversed(token.doc[:token.i]):
        if t._.template_tag is not None:
            if 'temp_var_' in t._.template_tag:
                left_context = 'temp_var'
            else:
                left_context = t._.template_tag
            break

    if token.i + 1 < len(token.doc):
        for t in token.doc[token.i + 1:]:
            if t._.template_tag is not None:
                if 'temp_var_' in t._.template_tag:
                    right_context = 'temp_var'
                else:
                    right_context = t._.template_tag
                break

    return left_context, right_context


def tag_encoding(doc, tags, exclude=None):
    """Tag encoding of a doc"""
    encoding = [0] * len(tags)
    for token in doc:
        if token._.template_tag is not None and 'temp_var_' not in token._.template_tag:
            encoding[tags.index(token._.template_tag)] = 1
    if exclude is list:
        for i in exclude:
            encoding[tags.index(i)] = 0
    elif exclude is str:
        encoding[exclude] = 0
    return encoding


def tag_type_encoding(doc, tags_to_types, tag_types, exclude=None):
    """Tag type encoding of a doc"""
    tag_type_counts = Counter()
    encoding = [0] * len(tag_types)
    for token in doc:
        if token._.template_tag is not None and 'temp_var_' not in token._.template_tag:
            tag_type_counts[tags_to_types[token._.template_tag]] += 1
    if exclude is list:
        for i in exclude:
            if tag_type_counts[tags_to_types[i]] > 0:
                tag_type_counts[tags_to_types[i]] -= 1
    elif exclude is str:
        if tag_type_counts[tags_to_types[exclude]] > 0:
            tag_type_counts[tags_to_types[exclude]] -= 1

    for idx, t in enumerate(tag_types):
        encoding[idx] = tag_type_counts[t]
    return encoding


def chunker(doc, data_col_to_type):
    def set_span_record_type(record_span):
        for t in record_span:
            if t._.template_tag is not None:
                record_span._.record_type = data_col_to_type[t._.template_tag]
                break
        return record_span

    chunks = []
    chunk_start = 0
    chunk_end = 0
    for token in doc:
        if (token.pos_ == 'ADP' or token.pos_ == 'VERB') and (
                token.i == 0 or doc[token.i - 1].pos_ not in ['ADP', 'VERB']):
            record_span = doc[chunk_start: token.i]
            record_span = set_span_record_type(record_span)
            chunks.append(record_span)
            chunk_start = token.i
            chunk_end = token.i

    record_span = doc[chunk_start: len(doc)]
    record_span = set_span_record_type(record_span)
    chunks.append(record_span)

    final_chunks = [chunks[0]]
    # print(final_chunks[-1]._.record_type, '\t\t', final_chunks[-1])
    for chunk in chunks[1:]:
        # print(chunk._.record_type, '\t\t', chunk)
        if (
                ((final_chunks[-1]._.record_type is not None and chunk._.record_type is None) or
                 (final_chunks[-1]._.record_type == chunk._.record_type))  # and
                # chunk[0].pos_ == 'ADP'
        ):
            new_chunk = doc[final_chunks[-1].start: chunk.end]
            new_chunk._.record_type = final_chunks[-1]._.record_type
            final_chunks[-1] = new_chunk

        elif final_chunks[-1]._.record_type is None and chunk._.record_type is not None:
            new_chunk = doc[final_chunks[-1].start: chunk.end]
            new_chunk._.record_type = chunk._.record_type
            final_chunks[-1] = new_chunk

        else:
            final_chunks.append(chunk)

    return final_chunks


class GenerateTemplates:

    def __init__(self, nlp, teams_file, vectorizer=None, clf=None):
        """
        Template features, training, and generation

        :param nlp: spacy model
        :param teams_file: path to teams file
        :param vectorizer: feature veotorizer
        :param clf: classifier model
        """
        self.nlp = nlp
        self.teams_file = teams_file
        self.data_cols = [
            'player_name', 'player_position',
            'team', 'week', 'game_dow', 'opp', 'away_game', 'team_score', 'opp_score',
            'pass_attempts', 'pass_completions', 'pass_percent', 'pass_yards', 'pass_ya', 'pass_td', 'pass_int',
            'pass_sack', 'pass_rate',
            'rush_attempts', 'rush_yards', 'rush_avg', 'rush_td',
            'receptions', 'rec_yards', 'rec_avg', 'rec_td', 'rec_targets', 'rec_yac'
        ]
        self.data_col_types = [
            'player', 'player',
            'game', 'game', 'game', 'game', 'game', 'game', 'game',
            'passing', 'passing', 'passing', 'passing', 'passing', 'passing', 'passing', 'passing', 'passing',
            'rushing', 'rushing', 'rushing', 'rushing',
            'receptions', 'receptions', 'receptions', 'receptions', 'receptions', 'receptions'
        ]
        self.data_col_idx = dict([(v, k) for k, v in enumerate(self.data_cols)])
        self.idx_data_col = dict([(k, v) for k, v in enumerate(self.data_cols)])
        self.data_col_to_type = dict([(k, v) for k, v in zip(self.data_cols, self.data_col_types)])
        self.teams, self.id_team_dict, self.team_id_dict = get_teams(self.teams_file)
        if vectorizer is not None and clf is not None:
            self.create_prediction_func(vectorizer, clf)
        else:
            self.prediction_func = None

    def template_tagging(self, doc, inverted_news_dict, training=True):
        assert training is True or self.prediction_func is not None, \
            'If training is False then prediction function must be instantiated. Call "create_prediction_func"'
        ambiguous_placeholder_count = 0
        ambiguous_placeholder_dict = {}

        for token in doc:
            if token.text in inverted_news_dict:
                if type(inverted_news_dict[token.text]) is list:
                    if training:
                        ambiguous_placeholder_dict['temp_var_{}'.format(ambiguous_placeholder_count)] = \
                            inverted_news_dict[token.text]
                        ambiguous_placeholder_count += 1
                        token._.template_tag = 'temp_var_' + str(ambiguous_placeholder_count)
                    else:
                        pred = self.prediction_func(token, inverted_news_dict[token.text])
                        token._.template_tag = pred
                else:
                    token._.template_tag = inverted_news_dict[token.text]

        if training:
            return doc, ambiguous_placeholder_dict
        else:
            return doc

    def doc_to_template(self, doc):
        tagged_text = ''
        for token in doc:
            if token._.template_tag is not None:
                tagged_text += '${' + token._.template_tag + '}'
            else:
                tagged_text += token.text.replace('$', '$$')
            tagged_text += token.text_with_ws[len(token.text):]
        return Template(tagged_text)

    def _create_prediction_func(self, vectorizer, clf):
        def prediction(token, tag_choices):
            bigrams = []
            for bigram in get_context(token, 2):
                if len(bigram) != 0:
                    bigrams.append(' '.join(bigram))

            probs = clf.predict_proba(vectorizer.transform([bigrams]))

            max_val = -np.inf
            max_val_tag = None
            for tag in tag_choices:
                try:
                    tag_idx = clf.classes_.tolist().index(self.data_col_idx[tag])
                    if probs[0, tag_idx] > max_val:
                        max_val = probs[0, tag_idx]
                        max_val_tag = tag
                except ValueError:
                    # Value in data columns that wasn't seen in training data
                    pass
            return max_val_tag

        return prediction

    def create_prediction_func(self, vectorizer, clf):
        self.prediction_func = self._create_prediction_func(vectorizer, clf)

    def doc_preprocessing(self, news_stats_df):
        for row in news_stats_df.iterrows():
            news_dict = row[1].to_dict()
            inverted_news_dict = create_inverted_news_dict(news_dict, self.data_cols, self.team_id_dict,
                                                           self.id_team_dict)

            normalized_text = text_normalization(news_dict['report'])

            doc = self.nlp(normalized_text)
            #for e in doc.ents:
            #    if e.label_ in ['NFL_PLAYER', 'NFL_TEAM']:
            #        e.merge()

            yield row[0], doc, news_dict, inverted_news_dict

    def create_training_data(self, news_stats_df, output_file=None):
        """
        Creates features for training a template slot disambiguation model

        :param news_stats_df: pandas dataframe with news and stats
        :param output_file: path to output file
        :return: token_training_set (list of tuples)
            df index, ngram_context, tag_context, tag_encoding, tag_type_encoding, relative position in doc,
            doc length, correct tag
        """
        news_reports = []
        output_templates = []
        token_training_set = []

        for idx, doc, news_dict, inverted_news_dict in self.doc_preprocessing(news_stats_df):
            doc, _ = self.template_tagging(doc, inverted_news_dict)
            news_template = self.doc_to_template(doc)

            for token in doc:
                if token._.template_tag is not None and 'temp_var_' not in token._.template_tag:
                    token_training_set.append(
                        (idx,
                         get_context(token, 2),
                         get_context_tags(token),
                         tag_encoding(doc, self.data_cols, exclude=[token._.template_tag]),
                         tag_type_encoding(doc, self.data_col_to_type,
                                           ['player', 'game', 'passing', 'rushing', 'receptions']),
                         token.i / len(doc),
                         len(doc),
                         token._.template_tag)
                    )

            news_reports.append(news_dict['report'])
            output_templates.append(news_template.template)

        if output_file is not None:
            output_df = pd.DataFrame({'reports': news_reports, 'templates': output_templates})
            output_df.to_csv(output_file, index=False)
            print('Output intermediate templates to ({})'.format(output_file))
        return token_training_set

    def template_transformer(self, news_stats_df, output_file=None):
        """
        Tags template slots and disambiguates unknown slots

        :param news_stats_df: pandas dataframe with news and stats
        :param output_file: path to output file
        :return:
        """
        assert self.prediction_func is not None, \
            'prediction function must be instantiated. Call "create_prediction_func"'
        news_reports = []
        output_templates = []
        chunks_list = []
        template_chunks_list = []

        for idx, doc, news_dict, inverted_news_dict in self.doc_preprocessing(news_stats_df):
            doc = self.template_tagging(doc, inverted_news_dict, training=False)
            news_template = self.doc_to_template(doc)
            chunks = chunker(doc, self.data_col_to_type)
            chunks_list.append(str([c.text_with_ws for c in chunks]))
            template_chunks_list.append(str([self.doc_to_template(c).template for c in chunks]))

            news_reports.append(news_dict['report'])
            output_templates.append(news_template.template)

        if output_file is not None:
            output_df = pd.DataFrame({'report': news_reports, 'templates': output_templates,
                                      'report_chunks': chunks_list, 'template_chunks': template_chunks_list})
            output_df.to_csv(output_file, index=False)
            print('Output templates to ({})'.format(output_file))
        return output_templates

    def template_chunker(self, doc):
        pass
