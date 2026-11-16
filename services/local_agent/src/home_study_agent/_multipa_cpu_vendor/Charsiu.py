# Derived from yuwchen/MultiPA commit a8d83ec1d80f9db60e6d32227cb1c562ee5e6022.
# MIT; see LICENSE.txt. No weight permission or calibration is conferred.
import torch
import numpy as np
from itertools import groupby
from .models import Wav2Vec2ForFrameClassification
from .utils import duration2textgrid, forced_align, seq2duration, word2textgrid
from .processors import CharsiuPreprocessor_en
class charsiu_aligner:
    
    def __init__(self, 
                 lang='en', 
                 sampling_rate=16000, 
                 device=None,
                 recognizer=None,
                 processor=None, 
                 tokenizer_path=None,
                 resolution=0.01):
                
        self.lang = lang 
        
        if processor is not None:
            self.processor = processor
        else:
            if self.lang != 'en' or tokenizer_path is None:
                raise ValueError('PET_MULTIPA_ENGLISH_LOCAL_TOKENIZER_REQUIRED')
            self.charsiu_processor = CharsiuPreprocessor_en(tokenizer_path)
        
        
        
        self.resolution = resolution
        
        self.sr = sampling_rate
        
        self.recognizer = recognizer
        
        if device is None:
            self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self.device = device

    
    def _freeze_model(self):
        self.aligner.eval().to(self.device)
        if self.recognizer is not None:
            self.recognizer.eval().to(self.device)
    
    
    
    def align(self,audio,text):
        raise NotImplementedError()
        
        
        
    def serve(self,audio,save_to,output_format='variable',text=None):
        raise NotImplementedError()
        
    
    def _to_textgrid(self,phones,save_to):
        '''
        Convert output tuples to a textgrid file

        Parameters
        ----------
        phones : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        '''
        duration2textgrid(phones,save_path=save_to)
        print('Alignment output has been saved to %s'%(save_to))
    
    
    
    def _to_tsv(self,phones,save_to):
        '''
        Convert output tuples to a tab-separated file

        Parameters
        ----------
        phones : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        '''
        with open(save_to,'w') as f:
            for start,end,phone in phones:
                f.write('%s\t%s\t%s\n'%(start,end,phone))
        print('Alignment output has been saved to %s'%(save_to))


class charsiu_forced_aligner(charsiu_aligner):
    
    def __init__(self, aligner, sil_threshold=4, **kwargs):
        super(charsiu_forced_aligner, self).__init__(**kwargs)
        self.aligner = Wav2Vec2ForFrameClassification.from_pretrained(aligner, local_files_only=True)
        self.sil_threshold = sil_threshold
        
        self._freeze_model()
        
        
    def align(self, audio, text):
        '''
        Perform forced alignment

        Parameters
        ----------
        audio : np.ndarray [shape=(n,)]
            time series of speech signal
        text : str
            The transcription

        Returns
        -------
        A tuple of aligned phones in the form (start_time, end_time, phone)

        '''
        audio = self.charsiu_processor.audio_preprocess(audio,sr=self.sr)
        audio = torch.Tensor(audio).unsqueeze(0).to(self.device)
        phones, words = self.charsiu_processor.get_phones_and_words(text)
        phone_ids = self.charsiu_processor.get_phone_ids(phones)

        with torch.no_grad():
            out = self.aligner(audio)

        cost = torch.softmax(out.logits,dim=-1).detach().cpu().numpy().squeeze()
        sil_mask = self._get_sil_mask(cost)
        
        nonsil_idx = np.argwhere(sil_mask!=self.charsiu_processor.sil_idx).squeeze()
        if nonsil_idx is None:
            raise Exception("No speech detected! Please check the audio file!")
        
        aligned_phone_ids = forced_align(cost[nonsil_idx,:],phone_ids[1:-1])

        aligned_phones = [self.charsiu_processor.mapping_id2phone(phone_ids[1:-1][i]) for i in aligned_phone_ids]

        pred_phones_ori = self._merge_silence(aligned_phones,sil_mask)
        
        pred_phones = seq2duration(pred_phones_ori,resolution=self.resolution)

        pred_words, word_phone_map = self.charsiu_processor.align_words(pred_phones, phones, words, return_map=True)

        ## get the predicted probability of output phone
        counter = 0
        pred_prob = np.empty((len(pred_phones),cost.shape[1]))
        group_idx = 0
        for _, group in groupby(pred_phones_ori):
            length = len(list(group))
            pred_prob[group_idx,:] = np.mean(cost[counter:counter+length,:], axis=0)
            counter += length
            group_idx+=1

        final_phones = [the_phone[2] for the_phone in pred_phones ]
        phone_ids = self.charsiu_processor.mapping_phone2id(final_phones)

        return pred_phones, pred_words, words, pred_prob, phone_ids, word_phone_map
    
    
    def serve(self,audio,text,save_to,output_format='textgrid'):
        '''
         A wrapper function for quick inference
    
        Parameters
        ----------
        audio : TYPE
            DESCRIPTION.
        text : TYPE, optional
            DESCRIPTION. The default is None.
        output_format : str, optional
            Output phone-taudio alignment as a "tsv" or "textgrid" file. 
            The default is 'textgrid'.
    
        Returns
        -------
        None.
    
        '''
        phones, words = self.align(audio,text)

        if output_format == 'tsv':
            if save_to.endswith('.tsv'):
                save_to_phone = save_to.replace('.tsv','_phone.tsv')
                save_to_word = save_to.replace('.tsv','_word.tsv')
            else:
                save_to_phone = save_to + '_phone.tsv'
                save_to_word = save_to + '_word.tsv'
            
            self._to_tsv(phones, save_to_phone)
            self._to_tsv(words, save_to_word)
            
        elif output_format == 'textgrid':
            self._to_textgrid(phones, words, save_to)
        else:
            raise Exception('Please specify the correct output format (tsv or textgird)!')    

    def _to_textgrid(self,phones,words,save_to):
        '''
        Convert output tuples to a textgrid file

        Parameters
        ----------
        phones : TYPE
            DESCRIPTION.

        Returns
        -------
        None.

        '''
        word2textgrid(phones,words,save_path=save_to)
        print('Alignment output has been saved to %s'%(save_to))
    

    def _merge_silence(self,aligned_phones,sil_mask):
        # merge silent and non-silent intervals
        pred_phones = []
        count = 0
        for i in sil_mask:
            if i==self.charsiu_processor.sil_idx:
                pred_phones.append('[SIL]')
            else:
                pred_phones.append(aligned_phones[count])
                count += 1
        assert len(pred_phones) == len(sil_mask)
        return pred_phones


  
    def _get_sil_mask(self,cost):
        # single out silent intervals
        
        preds = np.argmax(cost,axis=-1)
        sil_mask = []
        for key, group in groupby(preds):
            group = list(group)
            if  (key==self.charsiu_processor.sil_idx and len(group)<self.sil_threshold):
                sil_mask += [-1 for i in range(len(group))]
            else:
                sil_mask += group

        return np.array(sil_mask)

