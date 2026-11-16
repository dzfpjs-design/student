"""Model loading is imported only inside the fixed private CPU worker."""
from contextlib import contextmanager
import hashlib,importlib,importlib.metadata,importlib.util,io,json,os,sys,wave
from pathlib import Path
from uuid import uuid4
from .pet_multipa_epoch import need

def _read(path, limit):
    need(path.is_file() and not path.is_symlink(), 'REGULAR_LOCAL_FILE_REQUIRED')
    need(0 < path.stat().st_size <= limit, 'FILE_BOUND')
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    need(0 < len(value) <= limit, 'FILE_BOUND')
    return value


@contextmanager
def _no_nltk_download(nltk):
    original = nltk.download
    def denied(*args, **kwargs):
        raise ValueError('PET_MULTIPA_OFFLINE_RESOURCE_DOWNLOAD_FORBIDDEN')
    nltk.download = denied
    try:
        yield
    finally:
        nltk.download = original


class WorkerCPUModel:
    """Fixed original short-file inference; construction never downloads assets."""
    def __init__(self, lease):
        self.lease = lease
        lease.check()
        self.spec = dict(roles={k: lease.path(k) for k in lease.roles}, order=lease.order)
        need(os.environ.get('HF_HUB_OFFLINE') == '1'
             and os.environ.get('TRANSFORMERS_OFFLINE') == '1', 'OFFLINE_WORKER_ENVIRONMENT_REQUIRED')
        # A dedicated, explicitly configured local worker owns these dependency
        # globals. This object is not automatically loaded by the HTTP app.
        versions = json.loads(lease.authority['dependency-lock.json'])
        need(type(versions) is dict and bool(versions), 'DEPENDENCY_LOCK_REQUIRED')
        for package, version in versions.items():
            need(importlib.metadata.version(package) == version, 'DEPENDENCY_VERSION_CHANGED')
        nltk = importlib.import_module('nltk')
        nltk.data.path[:] = [str(self.spec['roles']['nltk_directory'])]
        nltk.data.find('corpora/cmudict')
        nltk.data.find('taggers/averaged_perceptron_tagger')
        with _no_nltk_download(nltk):
            torch = importlib.import_module('torch')
            torch.set_num_threads(1)
            self.torch = torch
            fairseq = importlib.import_module('fairseq')
            whisper = importlib.import_module('whisper')
            roberta = importlib.import_module('fairseq.models.roberta')
            vendor = __package__ + '._multipa_instance_' + uuid4().hex
            source = Path(__file__).parent / '_multipa_cpu_vendor'
            module_spec = importlib.util.spec_from_file_location(
                vendor, source / '__init__.py', submodule_search_locations=[str(source)])
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[vendor] = module
            module_spec.loader.exec_module(module)
            utility = importlib.import_module(vendor + '.utils_assessment')
            # Preserve the upstream set->list algorithm, compare it to the trusted
            # training order, and fail before loading weights on any column drift.
            utility.configure_phonemes(self.spec['order'])
            predictor = importlib.import_module(vendor + '.model_assessment').PronunciationPredictor
            aligner = importlib.import_module(vendor + '.Charsiu').charsiu_forced_aligner
            self.api = importlib.import_module(vendor + '.api')
            r = self.spec['roles']; device = torch.device('cpu')
            self.word_model = roberta.RobertaModel.from_pretrained(
                str(r['roberta_directory']), checkpoint_file='model.pt', bpe='gpt2',
                gpt2_encoder_json=str(r['roberta_encoder_json']),
                gpt2_vocab_bpe=str(r['roberta_vocab_bpe'])).to(device).eval()
            self.whisper_s = whisper.load_model(str(r['whisper_medium_en']), device='cpu').eval()
            self.whisper_w = whisper.load_model(str(r['whisper_base_en']), device='cpu').eval()
            aligned = aligner(aligner=str(r['charsiu_directory']), device='cpu',
                              tokenizer_path=str(r['tokenizer_directory']))
            ssl, _, _ = fairseq.checkpoint_utils.load_model_ensemble_and_task([str(r['hubert_checkpoint'])])
            assessment = predictor(ssl[0].to(device).eval(), 768, 768).to(device).eval()
            assessment.load_state_dict(torch.load(str(r['multipa_checkpoint']), map_location=device,
                                                  weights_only=True), strict=True)
            self.api.device = device
            self.api.word_model = self.word_model
            self.api.aligment_model = aligned
            self.api.assessment_model = assessment

    def predict_reference_segmented(self, path, reference, *, deadline):
        """Versioned long lane; full authoritative reference, raw per-segment only."""
        import copy, math, time
        from .pet_multipa_reference import validate_text
        from .pet_multipa_reference_segments import WindowedEmissions, plan, validate_result, alignment_bound, MAX_BYTES, MAX_FRAMES, SHORT_FRAMES, RESULT, ADAPTER
        reference=validate_text(reference)
        need(type(deadline)in(int,float)and math.isfinite(deadline),'REFERENCE_WORKER_BUDGET_REQUIRED')
        def check():
            self.lease.check()
            need(time.monotonic()<deadline,'WORKER_TIMEOUT')
        check();before=_read(path,MAX_BYTES)
        with wave.open(io.BytesIO(before),'rb')as wav:
            frames=wav.getnframes()
            need(wav.getnchannels()==1 and wav.getsampwidth()==2 and wav.getframerate()==16000 and wav.getcomptype()=='NONE'and SHORT_FRAMES<=frames<=MAX_FRAMES,'REFERENCE_LONG_PCM_REQUIRED')
        from .pet_multipa_reference_segments import budget
        allowed_work=budget(self.lease.approved,frames)
        need(deadline<=time.monotonic()+allowed_work,'REFERENCE_WORKER_BUDGET_REQUIRED')
        with self.torch.inference_mode():
            audio,rate=self.api.torchaudio.load(str(path));check()
            need(rate==16000 and tuple(audio.shape)==(1,frames),'REFERENCE_LONG_PCM_REQUIRED')
            audio=self.torch.reshape(audio,(-1,))
            normalized=self.api.convert_num_to_word(self.api.remove_pun_except_apostrophe(reference['text']).lower())
            need(type(normalized)is str and normalized.isascii()and bool(normalized.strip()),'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
            aligner=copy.copy(self.api.aligment_model)
            emission=WindowedEmissions(self.api.aligment_model.aligner,self.torch,check)
            need(type(aligner.resolution)in(int,float)and math.isfinite(aligner.resolution)and abs(aligner.resolution-emission.step/16000)<1e-12,'REFERENCE_ALIGNMENT_GRID_REQUIRED')
            expected_words=aligner.charsiu_processor._get_words(normalized)
            need(type(expected_words)is list and 1<=len(expected_words)<=2048 and all(type(w)is str and 0<len(w)<=256 for w in expected_words),'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
            phones,phone_words=aligner.charsiu_processor.get_phones_and_words(normalized);check()
            need(phone_words==expected_words,'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
            alignment_bound(frames,emission.step,emission.field,aligner.charsiu_processor.get_phone_ids(phones));check()
            aligner.aligner=emission
            alignment=aligner.align(audio=audio.numpy(),text=normalized);check()
            need(type(alignment)in(tuple,list)and len(alignment)==6 and alignment[2]==expected_words,'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
            pieces=plan(frames,expected_words,alignment[1]);del alignment
            rows=[]
            for piece in pieces:
                check();chunk=audio[piece['start_frame']:piece['end_frame']]
                # The unchanged original API normalizes GT again. Check that
                # round trip before ASR/features: no token may be lost or split.
                piece_text=self.api.convert_num_to_word(self.api.remove_pun_except_apostrophe(piece['text']).lower())
                piece_phones,piece_words=aligner.charsiu_processor.get_phones_and_words(piece_text);check()
                need(piece_words==expected_words[piece['start_word']:piece['end_word']],'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
                alignment_bound(piece['end_frame']-piece['start_frame'],emission.step,emission.field,aligner.charsiu_processor.get_phone_ids(piece_phones));check()
                observed=self.api.get_transcript(chunk,self.whisper_w);check()
                need(type(observed)is str and 0<len(observed.encode('utf-8'))<=16000,'REFERENCE_SEGMENTED_RESULT_REJECTED')
                observed_text=self.api.convert_num_to_word(self.api.remove_pun_except_apostrophe(observed).lower())
                observed_phones,_=aligner.charsiu_processor.get_phones_and_words(observed_text);check()
                # The original feature extractor also aligns observed ASR. It
                # is never GT, but its dense matrix needs the same hard bound.
                alignment_bound(piece['end_frame']-piece['start_frame'],emission.step,emission.field,aligner.charsiu_processor.get_phone_ids(observed_phones));check()
                values=self.api.inference_one_seg(piece['text'],observed,chunk);check()
                need(type(values)in(tuple,list)and len(values)==8,'REFERENCE_SEGMENTED_RESULT_REJECTED')
                labels=[str(row[2])for row in values[7]]
                need(labels==expected_words[piece['start_word']:piece['end_word']],'REFERENCE_FULL_WORD_COVERAGE_REQUIRED')
                sentence={key:value.cpu().detach().numpy()[0].item()for key,value in zip(('uttr_acc','uttr_fluency','uttr_prosodic','uttr_total'),values[:4])}
                words={key:value.cpu().detach().numpy()[0].tolist()for key,value in zip(('word_acc','word_stress','word_total'),values[4:7])}
                spans=[[piece['start_frame']+round(float(row[0])*16000),piece['start_frame']+round(float(row[1])*16000)]for row in values[7]]
                rows.append({**{k:piece[k]for k in('start_frame','end_frame','start_word','end_word')},'sentence':sentence,'words':words,'word_frames':spans})
        check();need(_read(path,MAX_BYTES)==before,'INPUT_CHANGED_DURING_INFERENCE')
        return validate_result(dict(schema=RESULT,adapter=ADAPTER,audio_sha256=hashlib.sha256(before).hexdigest(),reference_sha256=reference['text_sha256'],reference_utf8_bytes=reference['text_utf8_bytes'],frames=frames,reference_word_count=len(expected_words),alignment_frame_count=(frames-emission.field)//emission.step+1,segments=rows))

    def predict_reference_original(self, path, reference):
        """Separate short-reference adapter; learned outputs remain uncalibrated.

        The existing upstream inference_one_seg explicitly receives the current
        authoritative reference as GT and one observed ASR as its comparison.
        Its original normalization/alignment/features are unchanged. Neither
        source words nor recognized words leave this private worker result.
        """
        from .pet_multipa_reference import validate_text, validate_result, REFERENCE_ADAPTER, REFERENCE_RESULT
        reference = validate_text(reference)
        before = _read(path, 4194304)
        with wave.open(io.BytesIO(before), 'rb') as wav:
            frames = wav.getnframes()
            need(wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000 and wav.getcomptype() == 'NONE' and 0 < frames < 240000, 'ORIGINAL_SHORT_PCM_REQUIRED')
        with self.torch.inference_mode():
            audio, sample_rate = self.api.torchaudio.load(str(path))
            need(sample_rate == 16000 and tuple(audio.shape) == (1, frames), 'ORIGINAL_SHORT_PCM_REQUIRED')
            audio = self.torch.reshape(audio, (-1,))
            observed = self.api.get_transcript(audio, self.whisper_w)
            values = self.api.inference_one_seg(reference['text'], observed, audio)
        need(type(values) in (tuple,list) and len(values) == 8, 'REFERENCE_RESULT_REJECTED')
        sentence = {}
        for key, value in zip(('uttr_acc','uttr_fluency','uttr_prosodic','uttr_total'), values[:4]):
            sentence[key] = value.cpu().detach().numpy()[0].item()
        words = {key:value.cpu().detach().numpy()[0].tolist() for key,value in zip(('word_acc','word_stress','word_total'),values[4:7])}
        # Only frame positions survive. The upstream aligned word text is never
        # part of the result/host DB, and timing is not a correctness decision.
        spans = [[int(float(row[0])*16000),int(float(row[1])*16000)] for row in values[7]]
        need(_read(path, 4194304) == before, 'INPUT_CHANGED_DURING_INFERENCE')
        self.lease.check()
        return validate_result(dict(schema=REFERENCE_RESULT, adapter=REFERENCE_ADAPTER, audio_sha256=hashlib.sha256(before).hexdigest(), reference_sha256=reference['text_sha256'], reference_utf8_bytes=reference['text_utf8_bytes'], frames=frames, sentence=sentence, words=words, word_frames=spans))

    def predict_original(self, path):
        before = _read(path, 4194304)
        with wave.open(io.BytesIO(before), 'rb') as wav:
            need(wav.getnchannels() == 1 and wav.getsampwidth() == 2
                 and wav.getframerate() == 16000 and wav.getcomptype() == 'NONE'
                 and 0 < wav.getnframes() < 240000, 'ORIGINAL_SHORT_PCM_REQUIRED')
        with self.torch.inference_mode():
            result = self.api.predict_one_file(str(path), self.whisper_s, self.whisper_w, self.word_model)
        need(_read(path, 4194304) == before, 'INPUT_CHANGED_DURING_INFERENCE')
        self.lease.check()
        # Convert real numpy scalar/vector outputs, without score normalization,
        # clamping, replacing transcripts or substituting a human review.
        need(type(result) is dict and set(result) == {
            'uttr_acc', 'uttr_fluency', 'uttr_prosodic', 'uttr_total', 'word_acc',
            'word_stress', 'word_total', 'word_text', 'transcript_S', 'transcript_W'}, 'UPSTREAM_RESULT_REQUIRED')
        converted = dict(result)
        for key in ('uttr_acc', 'uttr_fluency', 'uttr_prosodic', 'uttr_total'):
            value = converted[key]
            converted[key] = value.item() if hasattr(value, 'item') else value
        for key in ('word_acc', 'word_stress', 'word_total'):
            value = converted[key]
            converted[key] = value.tolist() if hasattr(value, 'tolist') else value
        return converted

