import os
import random
from copy import deepcopy

from inst_generator import Word, rvInstGenerator, PREFIX, MAIN, SUFFIX
from fuzzer.scheduler import ScheduledCorpus
from fuzzer.program_backends import (
    FUZZER_BACKEND_ISAFUZZ,
    ISAFuzzDifuzzBackend,
    normalize_fuzzer_backend,
)

""" Mutation phases """
GENERATION = 0
MUTATION   = 1
MERGE      = 2

""" Template versions """
P_M = 0
P_S = 1
P_U = 2

# V_M = 3
# V_S = 3
V_U = 3

templates = [ 'p-m', 'p-s', 'p-u',
              'v-u']

class simInput():
    def __init__(self, prefix: list, words: list, suffix: list, ints: list,
                 data_seed: int, template: int, structured_payload=None):
        self.prefix = prefix
        self.words = words
        self.suffix = suffix
        self.ints = ints

        self.num_prefix = len(prefix)
        self.num_words = len(words)
        self.num_suffix = len(suffix)

        self.data_seed = data_seed
        self.template = template
        self.structured_payload = structured_payload

    def save(self, name, data=[]):
        prefix_insts = self.get_prefix()
        insts = self.get_insts()
        suffix_insts = self.get_suffix()

        fd = open(name, 'w')
        fd.write('{}\n\n'.format(templates[self.template]))

        for inst in prefix_insts[:-1]:
            fd.write('{:<50}\n'.format(inst))

        for (inst, INT) in zip(insts, self.ints):
            fd.write('{:<50}{:04b}\n'.format(inst, INT))

        for inst in suffix_insts[:-1]:
            fd.write('{:<50}\n'.format(inst))

        if data:
            fd.write('data:\n')
            for word in data:
                fd.write('{:016x}\n'.format(word))

        fd.close()

    def get_seed(self):
        return self.data_seed

    def get_template(self):
        return self.template

    def get_prefix(self):
        insts = []
        for word in self.prefix:
            insts += word.get_insts()

        insts.append(PREFIX + '{}:'.format(self.num_prefix))
        return insts

    def get_insts(self):
        insts = []
        for word in self.words:
            insts += word.get_insts()

        insts.append(MAIN + '{}:'.format(self.num_words))
        return insts

    def get_suffix(self):
        insts = []
        for word in self.suffix:
            insts += word.get_insts()

        insts.append(SUFFIX + '{}:'.format(self.num_suffix))
        return insts


class rvMutator():
    def __init__(self, max_data_seeds=100, corpus_size=1000,
                 no_guide=False, target_module=None,
                 seed_scheduler='sgmu', target_rare_horizon=8,
                 target_max_energy=8, phase_policy='default',
                 fuzzer_backend='difuzzrtl', target_cost_aware=False,
                 target_high_decay_window=0):
        self.corpus_size = corpus_size
        self.scheduled_corpus = ScheduledCorpus(
            mode=seed_scheduler,
            cap=corpus_size,
            rare_horizon=target_rare_horizon,
            max_energy=target_max_energy,
            target_high_decay_window=target_high_decay_window,
            cost_aware=bool(target_cost_aware),
            rng=random,
        )
        self.corpus = self.scheduled_corpus.items
        self.target_module = target_module or None

        self.phases = [GENERATION, MUTATION, MERGE]
        self.phase = GENERATION

        self.num_prefix = 3
        self.num_words = 100
        self.num_suffix = 5

        self.max_nWords = 200
        self.no_guide = no_guide
        self.phase_policy = str(phase_policy or 'default').strip().lower()
        if self.phase_policy not in ['default', 'mutation_only']:
            self.phase_policy = 'default'

        self.max_data = max_data_seeds
        self.random_data = {}
        self.data_seeds = []

        self.inst_generator = rvInstGenerator('RV64G')
        self.fuzzer_backend = normalize_fuzzer_backend(fuzzer_backend)
        self.structured_backend = None
        self.last_parent_seed_id = None
        self.last_parent_main_words = 0
        self.last_parent_static_insts = 0
        if self.fuzzer_backend == FUZZER_BACKEND_ISAFUZZ:
            self.structured_backend = ISAFuzzDifuzzBackend(random)
            self.structured_backend.prepare()

    def target_new_bits(self, target_hit_bits):
        return self.scheduled_corpus.new_targets(target_hit_bits or set())

    def observe_target_yield_result(self, target_new_bits, admitted=False,
                                    cost=None):
        self.scheduled_corpus.observe(
            target_new_bits or set(),
            admitted=bool(admitted),
            cost=cost)

    def choose_corpus_entry(self):
        choice = self.scheduled_corpus.choose()
        if choice is None:
            return None, None
        return choice.item, choice.seed_id

    def _static_insts(self, sim_input):
        if sim_input is None:
            return 0
        return sum(
            int(getattr(word, 'len_insts', 0) or 0)
            for word in sim_input.prefix + sim_input.words + sim_input.suffix
        )

    def _remember_parent(self, sim_input, parent_seed_id):
        self.last_parent_seed_id = parent_seed_id
        self.last_parent_main_words = len(getattr(sim_input, 'words', []) or [])
        self.last_parent_static_insts = self._static_insts(sim_input)

    def get_last_selection_summary(self):
        selections = getattr(self.scheduled_corpus, 'pending_selections', [])
        selection = selections[-1] if selections else None
        if selection is None:
            return {
                'parent_seed_id': self.last_parent_seed_id
                if self.last_parent_seed_id is not None else '',
                'parent_main_words': self.last_parent_main_words,
                'parent_static_insts': self.last_parent_static_insts,
                'reason': '',
                'energy': 0,
                'opportunity': 0.0,
                'conversion': 0.0,
                'cost_norm': 1.0,
                'value': 0.0,
                'best_alt_value': 0.0,
            }
        return {
            'parent_seed_id': selection.seed_id,
            'parent_main_words': self.last_parent_main_words,
            'parent_static_insts': self.last_parent_static_insts,
            'reason': selection.reason,
            'energy': selection.energy,
            'opportunity': selection.opportunity,
            'conversion': selection.conversion,
            'cost_norm': selection.cost_norm,
            'value': selection.value,
            'best_alt_value': selection.best_alt_value,
        }

    def add_data(self, new_data=[]):
        if len(self.data_seeds) == self.max_data:
            seed = self.data_seeds.pop(0)
        else:
            seed = len(self.data_seeds)

        if new_data:
            self.random_data[seed] = new_data
        else:
            self.random_data[seed] = [ random.randint(0, 0xffffffffffffffff) for i in range(64 * 6)] # TODO, Num_data_sections = 6
        self.data_seeds.append(seed)

        return seed

    def update_data_seeds(self, seed):
        assert self.data_seeds.count(seed) == 1, \
            '{} entrie(s) of {} exist in Mutator data_seeds'. \
            format(self.data_seeds.count(seed), seed)

        idx = self.data_seeds.index(seed)
        self.data_seeds.pop(idx)
        self.data_seeds.append(seed)

    def read_label(self, line, tuples):
        label = line[:8].split(':')[0]
        label_num = int(label[2:])

        insts = []
        tuples.append((label_num, insts))

        return tuples

    def tuples_to_words(self, tuples, part):
        words = []

        for tup in tuples:
            label = tup[0]
            insts = tup[1]

            word = Word(label, insts)
            word.populate({}, part)

            words.append(word)

        return words

    def read_siminput(self, si_name):
        fd = open(si_name, 'r')
        lines = fd.readlines()
        fd.close()

        ints = []
        prefix_tuples = []
        word_tuples = []
        suffix_tuples = []
        data = []

        num_prefix = 0
        num_word = 0
        num_suffix = 0

        part = None
        tmp_tuples = None
        num_tmp = None

        template_word = lines.pop(0).split('\n')[0]
        template = templates.index(template_word)
        lines.pop(0)
        while True:
            try: line = lines.pop(0)
            except: break

            if 'data:' in line:
                part = None
                while True:
                    try: word = lines.pop(0)
                    except: break

                    data.append(int(word, 16))
                break
            elif line[:2] == PREFIX:
                part = PREFIX
                num_prefix += 1
                tmp_tuples = self.read_label(line, prefix_tuples)
                num_tmp = num_prefix

                tmp_tuples[num_tmp - 1][1].append(line[8:50])
            elif line[:2] == MAIN:
                part = MAIN
                num_word += 1
                tmp_tuples = self.read_label(line, word_tuples)
                num_tmp = num_word
               
                tmp_tuples[num_tmp - 1][1].append(line[8:50])
            elif line[:2] == SUFFIX:
                part = SUFFIX
                num_suffix += 1
                tmp_tuples = self.read_label(line, suffix_tuples)
                num_tmp = num_suffix

                tmp_tuples[num_tmp - 1][1].append(line[8:50])
            else:
                tmp_tuples[num_tmp - 1][1].append(line[8:50])

            if part == MAIN:
                ints.append(int(line[-5:-1], 2))

        prefix = self.tuples_to_words(prefix_tuples, PREFIX)
        words = self.tuples_to_words(word_tuples, MAIN)
        suffix = self.tuples_to_words(suffix_tuples, SUFFIX)

        data_seed = self.add_data(data)
        sim_input = simInput(prefix, words, suffix, ints, data_seed, template)
        data = self.random_data[data_seed]

        assert_intr = False
        if [ i for i in ints if i != 0 ]:
            assert_intr = True

        return (sim_input, data, assert_intr)

    def make_nop(self, sim_input, nop_mask, part):
        data_seed = sim_input.get_seed()
        prefix = sim_input.prefix
        words = sim_input.words
        suffix = sim_input.suffix
        ints = sim_input.ints
        template = sim_input.template

        if part == PREFIX: target = prefix
        elif part == MAIN: target = words
        else: target = suffix

        assert len(target) == len(nop_mask), \
            'Length of words and nop_mask are not equal'

        new_target = []
        for (word, mask) in zip(target, nop_mask):
            if mask:
                new_word = Word(word.label, ['nop'])
                new_word.populate({}, part)
                new_target.append(new_word)
            else:
                new_target.append(word)

        if part == PREFIX:
            min_input = simInput(new_target, words, suffix, ints, data_seed,
                                 template, sim_input.structured_payload)
        elif part == MAIN:
            new_ints = []
            k = 0
            for i in range(len(nop_mask)):
                if nop_mask[i]:
                    new_ints += [0] * new_target[i].len_insts
                else:
                    new_ints += [ ints[k + j] for j in range(new_target[i].len_insts) ]

                k += new_target[i].len_insts

            min_input = simInput(prefix, new_target, suffix, new_ints,
                                 data_seed, template,
                                 sim_input.structured_payload)
        else:
            min_input = simInput(prefix, words, new_target, ints, data_seed,
                                 template, sim_input.structured_payload)

        data = self.random_data[data_seed]
        return (min_input, data)

    def delete_nop(self, sim_input):
        data_seed = sim_input.get_seed()
        prefix = sim_input.prefix
        words = sim_input.words
        suffix = sim_input.suffix
        ints = sim_input.ints
        template = sim_input.template

        words_map = {}
        new_ints = []
        k = 0
        for (part, target) in zip([PREFIX, MAIN, SUFFIX], [prefix, words, suffix]):
            tmps = []
            for word in target:
                if word.insts != ['nop']:
                    new_word = deepcopy(word)
                    tmps.append(new_word)

                if part == MAIN:
                    if word.insts != ['nop']:
                        new_ints += ints[k:k+word.len_insts]
                    k += word.len_insts

            new_target = self.reset_labels(tmps, part)
            words_map[part] = new_target

        del_input = simInput(words_map[PREFIX], words_map[MAIN],
                             words_map[SUFFIX], new_ints, data_seed, template,
                             sim_input.structured_payload)
        data = self.random_data[data_seed]

        return (del_input, data)

    def update_corpus(self, corpus_dir, update_num=100):
        si_files = os.listdir(corpus_dir)

        num_files = len(si_files)
        start = max(num_files - update_num, 0)
        for i in range(start, num_files):
            try:
                (sim_input, _) = self.read_siminput(corpus_dir +
                                                    '/id_{}.si'.format(i))
                self.add_corpus(sim_input)
            except:
                continue

    def reset_labels(self, words, part):
        n = 0

        label_map = {}
        for (n, word) in enumerate(words):
            tup = word.reset_label(n, part)
            if tup:
                label_map[tup[0]] = tup[1]

        max_label = len(words)

        for word in words:
            word.repop_label(label_map, max_label, part)

        return words

    def mutate_words(self, seed_words, part, max_num):
        words = []

        for word in seed_words:
            rand = random.random()
            if rand < 0.5:
                words.append(word)
            elif rand < 0.75:
                words.append(word)
                new_word = self.inst_generator.get_word(part)
                words.append(new_word)

        words = words[0:max_num]
        words = self.reset_labels(words, part)

        return words

    def _make_structured_input(self, result):
        prefix, words, suffix, data, template, payload = result
        data_seed = self.add_data(data)
        i_len = sum(word.len_insts for word in words)
        ints = [0 for _ in range(i_len)]
        return (
            simInput(prefix, words, suffix, ints, data_seed, template,
                     structured_payload=payload),
            self.random_data[data_seed],
        )

    def _get_structured_generation(self, fixed_template=None):
        if self.structured_backend is None:
            return None
        self.last_parent_seed_id = None
        self.last_parent_main_words = 0
        self.last_parent_static_insts = 0
        result = self.structured_backend.generate(
            fixed_template=fixed_template, with_payload=True)
        return self._make_structured_input(result)

    def _get_structured_mutation(self):
        if self.structured_backend is None:
            return None
        seed_si, parent_seed_id = self.choose_corpus_entry()
        if seed_si is None:
            return None
        self._remember_parent(seed_si, parent_seed_id)
        payload = getattr(seed_si, 'structured_payload', None)
        result = self.structured_backend.mutate(
            payload, parent_key=parent_seed_id)
        if result is None:
            return None
        return self._make_structured_input(result)

    def get(self, assert_intr=False):
        i_len = 0
        prefix = []
        words = []
        suffix = []

        self.inst_generator.reset()

        data_seed = -1
        template = -1
        self.scheduled_corpus.begin_input()
        phase = self.phase
        if phase in [MUTATION, MERGE] and not self.corpus:
            phase = GENERATION

        if phase == GENERATION:
            if self.fuzzer_backend == FUZZER_BACKEND_ISAFUZZ:
                fixed_template = None if template == -1 else template
                structured = self._get_structured_generation(fixed_template)
                if structured is not None:
                    return structured
            for n in range(self.num_prefix):
                word = self.inst_generator.get_word(PREFIX)
                prefix.append(word)
            for n in range(self.num_words):
                word = self.inst_generator.get_word(MAIN)
                words.append(word)
            for n in range(self.num_suffix):
                word = self.inst_generator.get_word(SUFFIX)
                suffix.append(word)

        elif phase in [ MUTATION, MERGE ]:
            if self.fuzzer_backend == FUZZER_BACKEND_ISAFUZZ:
                structured = self._get_structured_mutation()
                if structured is not None:
                    return structured
            if phase == MUTATION:
                seed_si, _parent_seed_id = self.choose_corpus_entry()
                self._remember_parent(seed_si, _parent_seed_id)
                seed_prefix = deepcopy(seed_si.prefix)
                seed_words = deepcopy(seed_si.words)
                seed_suffix = deepcopy(seed_si.suffix)
                data_seed = seed_si.get_seed()
                template = seed_si.get_template()
            else:
                seed_words = []
                seed_si1, _parent_seed_id = self.choose_corpus_entry()
                seed_si2, _ = self.choose_corpus_entry()
                self._remember_parent(seed_si1, _parent_seed_id)

                seed_prefix = deepcopy(seed_si1.prefix)
                si1_words = deepcopy(seed_si1.words)
                si2_words = deepcopy(seed_si2.words)
                seed_suffix = deepcopy(seed_si1.suffix)
                idx = random.randint(0, min(len(si1_words),
                                            len(si2_words)))

                for i in range(idx):
                    seed_words.append(si1_words[i])
                for i in range(idx, len(si2_words)):
                    seed_words.append(si2_words[i])
                data_seed = seed_si1.get_seed()
                template = seed_si1.get_template()

            prefix = self.mutate_words(seed_prefix, PREFIX, self.num_prefix)
            words = self.mutate_words(seed_words, MAIN, self.max_nWords)
            suffix = self.mutate_words(seed_suffix, SUFFIX, self.num_suffix)

        for word in prefix:
            self.inst_generator.populate_word(word, len(prefix), PREFIX)

        max_label = len(words)
        for word in words:
            i_len += word.len_insts
            self.inst_generator.populate_word(word, max_label, MAIN)

        for word in suffix:
            self.inst_generator.populate_word(word, len(suffix), SUFFIX)

        ints = [ 0 for i in range(i_len) ]
        if assert_intr:
            idx = random.randint(0, min(len(ints), 10) - 1)
            INT = random.randint(0x1, 0xf)
            ints[idx] = INT

        if data_seed == -1:
            data_seed = self.add_data()
        else:
            self.update_data_seeds(data_seed)

        if template == -1:
            template = random.randint(0, V_U)

        sim_input = simInput(prefix, words, suffix, ints, data_seed, template)
        data = self.random_data[data_seed]

        return (sim_input, data)

    def update_phase(self, it):
        if (it + 1) < self.corpus_size / 10 or self.no_guide:
            self.phase = GENERATION
        elif self.phase_policy == 'mutation_only':
            self.phase = MUTATION
        else:
            rand = random.random()
            if rand < 0.1:
                self.phase = GENERATION
            elif rand < 0.55:
                self.phase = MUTATION
            else:
                self.phase = MERGE

    def add_corpus(self, sim_input, coverage=0, module_covs=None,
                   target_hit_bits=None, initial_cost=None):
        target_hit_bits = set(target_hit_bits or set())
        module_covs = module_covs or {}
        target_score = 0
        if self.target_module:
            target_score = int(module_covs.get(self.target_module, 0) or 0)
        elif target_hit_bits:
            target_score = len(target_hit_bits)
        target_new_bits = self.target_new_bits(target_hit_bits)
        added = self.scheduled_corpus.add(
            sim_input,
            targets=target_hit_bits,
            target_score=target_score,
            target_new_score=len(target_new_bits),
            total_score=int(coverage or 0),
            initial_cost=initial_cost,
        )
        if not added:
            return False

        self.num_words = min(self.num_words + 1, self.max_nWords)
        return True
