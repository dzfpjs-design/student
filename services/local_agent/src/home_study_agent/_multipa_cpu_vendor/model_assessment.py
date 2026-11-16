# Derived from yuwchen/MultiPA commit a8d83ec1d80f9db60e6d32227cb1c562ee5e6022.
# MIT; see LICENSE.txt. No weight permission or calibration is conferred.
import torch
import torch.nn as nn
import torch.nn.functional as F
class PronunciationPredictor(nn.Module):
    
    def __init__(self, ssl_model, ssl_out_dim, text_out_dim):
        super(PronunciationPredictor, self).__init__()
        self.ssl_model = ssl_model
        self.ssl_features = ssl_out_dim  # size of HuBERT embedding
        self.text_out_dim = text_out_dim # size of roberta embedding
        self.w_features_size = 10
        self.p_align_size = 9
        self.p_pred_size = 42
        self.phone_vector = 71*2

        hidden_word = self.ssl_features+self.text_out_dim*2+self.phone_vector+self.w_features_size+self.p_align_size+self.p_pred_size*2+1
        hidden_sen = 768+hidden_word

        self.output_accuracy = nn.Linear(hidden_sen, 1) 
        self.output_fluency = nn.Linear(hidden_sen, 1) 
        self.output_prosodic = nn.Linear(hidden_sen, 1) 
        self.output_total = nn.Linear(hidden_sen, 1)

        self.fusion_layer = nn.TransformerEncoderLayer(d_model=hidden_word, nhead=30)
        
        self.fusion_layer_word = nn.TransformerEncoderLayer(d_model=hidden_word, nhead=30)
        self.w_feature_layer = nn.TransformerEncoderLayer(d_model=self.w_features_size, nhead=5)
        self.p_feature_layer_align = nn.TransformerEncoderLayer(d_model=self.p_align_size, nhead=3)
        self.p_feature_layer_pred_gt = nn.TransformerEncoderLayer(d_model=self.p_pred_size, nhead=7)
        self.p_feature_layer_pred_asr = nn.TransformerEncoderLayer(d_model=self.p_pred_size, nhead=7)
        self.p_feature_layer_1 = nn.TransformerEncoderLayer(d_model=self.p_align_size+self.p_pred_size*2, nhead=3)
        self.p_feature_layer_2 = nn.TransformerEncoderLayer(d_model=self.p_align_size+self.p_pred_size*2, nhead=3)
        self.phonevector_layer = nn.TransformerEncoderLayer(d_model=self.phone_vector, nhead=2)

        self.word_acc = nn.Conv1d(hidden_word, 1, kernel_size=1)
        self.word_stress = nn.Conv1d(hidden_word, 1, kernel_size=1)
        self.word_total = nn.Conv1d(hidden_word, 1, kernel_size=1)

    def forward(self, wav, asr_word_embed, gt_word_embed, features_p, features_w, phonevector, word_phone_map, timesplit):
        
        res = self.ssl_model(wav, mask=False, features_only=True)
        wav_embedding_raw = res['x']
        
        ### align word-level features to the wavform
        batch_size  = gt_word_embed.shape[0]
        wav_aligned = wav_embedding_raw.new_zeros((gt_word_embed.shape[0], gt_word_embed.shape[1], self.ssl_features))
        for b_idx in range(batch_size):
            for w_idx in range(len(timesplit[b_idx])):
                start_point = timesplit[b_idx][w_idx][0] // 320
                end_point = timesplit[b_idx][w_idx][1] // 320
                if (end_point - start_point)==0:  # avoid predict nan because of no aligned wav segment                 
                    the_word = wav_embedding_raw[b_idx, start_point:start_point+1, :]
                else:
                    the_word = wav_embedding_raw[b_idx, start_point:end_point, :]
                aligned_wav_embed = the_word.mean(dim=0)
                wav_aligned[b_idx, w_idx, :] = aligned_wav_embed


        features_w = self.w_feature_layer(features_w)
        features_p = self.p_feature_layer_1(features_p)
        features_p[:,:,:self.p_align_size] = self.p_feature_layer_align(features_p[:,:,:self.p_align_size])
        features_p[:,:,self.p_align_size:self.p_align_size+self.p_pred_size] = self.p_feature_layer_pred_gt(features_p[:,:,self.p_align_size:self.p_align_size+self.p_pred_size])
        features_p[:,:,self.p_align_size+self.p_pred_size:] = self.p_feature_layer_pred_asr(features_p[:,:,self.p_align_size+self.p_pred_size:])

        # align phone-level features to word-level features
        features_p_aligned = features_p.new_zeros((gt_word_embed.shape[0], gt_word_embed.shape[1], self.p_align_size+self.p_pred_size*2))
        for b_idx in range(batch_size):
            for w_idx, p_list in enumerate(word_phone_map[b_idx]):
                features_p_aligned[b_idx, w_idx, :] = features_p[b_idx,p_list,:].mean(dim=0)

        features_p_aligned = self.p_feature_layer_2(features_p_aligned)
        phonevector = self.phonevector_layer(phonevector)
        fusion = torch.cat([wav_aligned, gt_word_embed, asr_word_embed, features_w, features_p_aligned, phonevector], dim=2)  
        fusion = F.pad(fusion, (0, 1), mode='constant') # expand one dimension because original feature size is a prime number

        fusion_word = self.fusion_layer_word(fusion)
        fusion_word = fusion_word.transpose(1, 2) 
        output_w_acc = self.word_acc(fusion_word)
        output_w_stress = self.word_stress(fusion_word)
        output_w_total = self.word_total(fusion_word)
        output_w_acc = output_w_acc.transpose(1,2).squeeze(2) 
        output_w_stress = output_w_stress.transpose(1,2).squeeze(2)
        output_w_total = output_w_total.transpose(1,2).squeeze(2)
        

        fusion = self.fusion_layer(fusion) 
        uttr_word = torch.mean(fusion, 1)
        wav_embedding = torch.mean(wav_embedding_raw, 1)
        uttr = torch.cat([wav_embedding, uttr_word], dim=1)
        output_A = self.output_accuracy(uttr)
        output_F = self.output_fluency(uttr)
        output_P = self.output_prosodic(uttr)
        output_T = self.output_total(uttr)
        

        return output_A.squeeze(1), output_F.squeeze(1), output_P.squeeze(1), output_T.squeeze(1), output_w_acc, output_w_stress, output_w_total 

