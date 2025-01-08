# Copyright (c) 2024-present, Royal Bank of Canada.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
from models.attention_modules import *
import math
from models.aggregator_modules import TransformerAggregator

##### Memory


class Memory(nn.Module):
    def __init__(self):
        super(Memory, self).__init__()

    def retrieve(self, x):
        """
        Arguments:
            x: [B, M, D] Tensor
        Returns:
            ret: [B, M, D] Tensor
        """
        raise NotImplementedError


class VanillaMemory(Memory):
    def __init__(self, d_model, nhead, dim_feedforward, dropout, norm_first):
        super(VanillaMemory, self).__init__()

        if norm_first:
            Norm = PreNorm
        else:
            Norm = PostNorm

        self.cross_attention = Norm(
            d_model,
            Attention(d_model, nhead=nhead, dim_head=d_model // nhead, dropout=dropout),
        )
        self.cross_attention_ff = Norm(
            d_model, FeedForward(d_model, dim_feedforward, dropout)
        )

    def setup_data(self, layer_data):
        self.layer_data = layer_data

    def reset(self):
        self.layer_data = None

    def retrieve(self, query_data):
        """
        Arguments:
            query_data: [B, M, D] Tensor
        Returns:
            ret: [B, M, D] Tensor
        """
        ca_output = self.cross_attention(
            query_data, key=self.layer_data, value=self.layer_data
        )
        ca_ff_output = self.cross_attention_ff(ca_output)
        return ca_ff_output


class TreeMemory(Memory):
    def __init__(
        self,
        d_model,
        nhead,
        dim_feedforward,
        dropout,
        norm_first,
        branch_factor,
        num_aggregation_layers,
        bptt,
        aggregator_type,
    ):
        super(TreeMemory, self).__init__()
        self.mlps = nn.ModuleList([nn.Linear(in_features=64, out_features=64, bias=False).to("cuda")  for _ in range(6)])
        self.mean_mlp_loss = {i:(0, 0.0) for i in range(6)}

        self.norm_first = norm_first
        if norm_first:
            Norm = PreNorm
            AttNorm = AttPreNorm
        else:
            Norm = PostNorm
            AttNorm = AttPostNorm

        # Check Notes for implementation
        self.branch_factor = branch_factor

        self.train_tree_data = ( # List representation of the tree
            [] # To be filled with type: (layer_data, layer_mask)
        )  

        if aggregator_type == "transformer":
            # Used to order the datapoints.
            self.aggregator = TransformerAggregator(
                num_aggregation_layers,
                d_model,
                nhead,
                dim_feedforward,
                dropout,
                norm_first,
                bptt,
            )
        else:
            raise NotImplementedError

        # The attention model and policy
        self.query_model = AttNorm(
            d_model,
            Attention(d_model, nhead=nhead, dim_head=d_model // nhead, dropout=0.), # Set the dropout of the policy to 0
        )
        self.query_ff = Norm(
            d_model,
            FeedForward(d_model, dim_feedforward=dim_feedforward, dropout=dropout),
        )


    def setup_data(self, layer_data):
        self.tree_generator(layer_data)

    def reset(self):
        del self.train_tree_data
        self.train_tree_data = []

    def pad_node_data(self, node_data):
        # Pad node data to make it easily handleable

        device = node_data.device
        B, N, D = node_data.shape

        k = (
            torch.floor(
                torch.log(torch.tensor(N - 1))
                / torch.log(torch.tensor(self.branch_factor))
            )
            .int()
            .item()
        )
        P = math.ceil(N / self.branch_factor**k)

        num_pad_nodes = (
            P * (self.branch_factor**k) - N
        )

        tree_data = torch.zeros(B, N + num_pad_nodes, D).to(device) 
        tree_data[:, :N, :] = node_data
        tree_data[:, N:, :] = (
            node_data[:, :num_pad_nodes, :] + 1e-5
        )  # Pad with real data and some buffer to relatively evenly split the data
        mask = torch.zeros(tree_data.shape[:-1], device=device).unsqueeze(
            -1
        )  # [B, N', 1]
        mask[:, :N, :] = 1  # Real data
        return tree_data, mask

    def tree_generator(self, node_data):
        # For efficiency, we avoid an explicit k-d tree construction and use a tensor representation of a tree structure.
        # This tree construction leverages the fact that the node_data was previously organized. 
        # An example of a tensor representation of the leaves is [2][2][2][2] (i.e., a 2x2x2x2 tensor)
        # Example 1: Indexing according to [0][1][1][0] refers to the leaf achieved by going left, right, right, left down the tree.
        # Example 2: Indexing according to [1][1][0][0] refers to the leaf achieved by going right, right, left, left down the tree.

        B, N, D = node_data.shape
        k = (
            torch.floor(
                torch.log(torch.tensor(N - 1))
                / torch.log(torch.tensor(self.branch_factor))
            )
            .int()
            .item()
        )  
        P = math.ceil(N / self.branch_factor**k)

        tree_depth_data, tree_depth_mask = self.pad_node_data(
            node_data
        )  # Pad the data to an easily organizable size

        tree_depth_data = tree_depth_data.reshape(
            B, *([self.branch_factor] * k), P, D
        )  # Construct the leaves
        tree_depth_mask = tree_depth_mask.reshape(B, *([self.branch_factor] * k), P, 1)

        self.bottom_up_aggregation(tree_depth_data, tree_depth_mask)

    def bottom_up_aggregation(self, tree_depth_data, tree_depth_mask):
        D = tree_depth_data.shape[-1]
        # tree_depth_data: [B, b, b, ..., b, P, D], tree_depth_mask: [B, b, b, ..., b, P, 1]
        self.train_tree_data = [(tree_depth_data, tree_depth_mask.detach())]

        # Perform the bottom-up aggregation
        while len(tree_depth_data.shape) > 2:  # Stops at [B, D]
            tmp_batch_size = np.prod(tree_depth_data.shape[:-2])
            branch_size = tree_depth_data.shape[-2]

            tmp_tree_depth_data = tree_depth_data.reshape(
                (tmp_batch_size, branch_size, D)
            )
            tmp_tree_depth_mask = tree_depth_mask.reshape(
                (tmp_batch_size, branch_size, 1)
            )

            computed_tree_depth_data = self.aggregator(
                tmp_tree_depth_data, tmp_tree_depth_mask
            ).squeeze(
                -2
            )  # [B..., D]

            computed_tree_depth_mask = tmp_tree_depth_mask.any(
                dim=-2
            ).float()  # if any of its children are not padding nodes, then it is not a padding node

            tree_depth_data = computed_tree_depth_data.reshape(
                (*(tree_depth_data.shape[:-2]), tree_depth_data.shape[-1])
            )  # [B, b, ..., b, D]
            tree_depth_mask = computed_tree_depth_mask.reshape(
                (*(tree_depth_mask.shape[:-2]), tree_depth_mask.shape[-1])
            )  # [B, b, ..., b, 1]

            self.train_tree_data.append((tree_depth_data, tree_depth_mask.detach()))

        self.train_tree_data = list(reversed(self.train_tree_data))

    def retrieve(self, query_data):
        entropy_att_scores_list = []
        log_branch_sel_prob_list = []

        pred_emb, entropy_att_scores_list, log_branch_sel_prob_list  = (
            self.tree_retrieval(query_data)
        )

        if self.norm_first:
            ret_emb = self.query_ff(pred_emb + query_data)
        else:
            ret_emb = self.query_ff(pred_emb)

        if not self.training:
            return ret_emb
        else:
            leaf_pred_emb = self.tree_leaves_retrieval(query_data)
            entropy_scores, log_action_probs = self.process_rl_terms(
                entropy_att_scores_list, log_branch_sel_prob_list
            )

            if self.norm_first:  # Standard PreNorm used in CA but this time for TCA
                leaf_ret_emb = self.query_ff(leaf_pred_emb + query_data)
            else:
                leaf_ret_emb = self.query_ff(leaf_pred_emb)

            return ret_emb, leaf_ret_emb, entropy_scores, log_action_probs, self.mean_mlp_loss
        
    def use_mlps(self, i, query_data, layer_data_embeddings):
        #self.check(i, layer_data_embeddings

        # run MLP model
        # (16, 2, 64)
        projected_layer_data_embdeddings = self.mlps[i](layer_data_embeddings)

        #self.check(i, projected_layer_data_embdeddings)
        #B b D, B M D -> B b M'
        decision_tensor = torch.einsum('bnd,bmd->bnm', projected_layer_data_embdeddings, query_data)

        # B b M -> B b 1
        linear_layer = nn.Linear(decision_tensor.shape[-1], 1).to("cuda")
        decision_tensor = linear_layer(decision_tensor)
        #self.check(i, decision_tensor)


        decision_tensor = rearrange(decision_tensor, 'B b 1 -> B 1 b')
        #self.check(i, level_search_att_weight_mean_nodes)
        return decision_tensor

    def track_mlp_loss(self, i, loss):
        # Update mean loss using the recursive formula
        count, mean = self.mean_mlp_loss[i]
        count += 1
        mean += (loss - mean) / count
        self.mean_mlp_loss[i] = (count, mean)

    def tree_retrieval(self, query_data):
        device = query_data.device
        batch_size, nQueries = query_data.shape[0], query_data.shape[1]
        B = batch_size
        #M = nQueries
        D = self.train_tree_data[0][0].shape[-1]

        filtered_tree_data = self.train_tree_data[1:]
        # query_data [B, M, D]

        entropy_att_scores_list = []
        log_branch_sel_prob_list = []
        selected_data_embeddings = [] # Array of [B*M, 1, D] (Stores selected nodes)
        selected_data_masks = [] # Array of [B*M, 1, 1] (Stores selected nodes' mask)
        
        for i in range(len(filtered_tree_data)):
            (layer_data_embeddings, layer_data_mask) = filtered_tree_data[i]

            layer_data_embeddings = torch.ones(B,  2, D).to("cuda")    # Initialized with ones
            layer_data_mask = torch.ones(B,  2, 1).to("cuda")     # Initialized with ones    

            if i == len(filtered_tree_data) - 1:
                selected_data_embeddings.append(layer_data_embeddings)
                selected_data_masks.append(layer_data_mask)
                break

            # TODO: adjust tree-construction --> only allow binary
            N_i = 2 #layer_data_embeddings.shape[1]

            if self.training:
                _, level_search_att_weight_mean_nodes, search_att_weight = self.query_model(
                    query_data,
                    key=layer_data_embeddings,
                    value=layer_data_embeddings,
                    src_mask=rearrange(layer_data_mask, 'B b 1 -> B 1 b'),
                    return_info=True,
                )

                level_search_att_weight_mean_nodes = torch.rand(B, 1, 2).to("cuda")

                query_data_copy = query_data.clone().detach()
                layer_data_embeddings_copy = layer_data_embeddings.clone().detach()
                level_search_att_weight_mean_nodes_copy = level_search_att_weight_mean_nodes.clone().detach()

                # Forward pass for the i-th MLP layer
                decision_tensor = self.use_mlps(i, query_data_copy, layer_data_embeddings_copy)
                loss = F.mse_loss(level_search_att_weight_mean_nodes_copy, decision_tensor)

                # Track the loss (optional)
                self.track_mlp_loss(i, loss)
                loss.backward()
            else:
                level_search_att_weight_mean_nodes = self.use_mlps(i, query_data, layer_data_embeddings)
                """_, level_search_att_weight_mean_nodes, search_att_weight = self.query_model(
                    flattened_query_data,
                    key=layer_data_embeddings,
                    value=layer_data_embeddings,
                    src_mask=rearrange(layer_data_mask, 'BM b 1 -> BM 1 b'),
                    return_info=True,
                )"""
                    
            # Select the next node to expand
            if self.training:
                # Stochastic selection
                selected_indices = torch.multinomial(level_search_att_weight_mean_nodes.flatten(0, 1), 1)
            else:  
                # Greedily (deterministically) select the nodes to expand
                selected_indices = level_search_att_weight_mean_nodes.flatten(0, 1).max(-1)[1].unsqueeze(-1)

            # Compute the mask for the selected/rejected nodes 
            # tree_search_level_embeddings = layer_data_embeddings.reshape(B, N_i, D)
            # Expand `selected_idx` to match the embedding dimensions
            selected_idx_expanded = selected_indices.unsqueeze(-1).expand(-1, -1, 64)  # Shape: (16, 1, 64)

            # Use `torch.gather` to collect embeddings based on `selected_idx`
            tree_search_level_embeddings = torch.gather(layer_data_embeddings, dim=1, index=selected_idx_expanded)  # Shape: (16, 1, 64)
            
            #tree_search_level_mask = (1 - F.one_hot(selected_indices, num_classes = N_i)).reshape(B, N_i, 1)
            tree_search_level_mask = (1 - rearrange(selected_indices, 'B 1 -> B 1 1'))

            # Add the level's node embeddings and mask

            selected_data_embeddings.append(tree_search_level_embeddings) # Add to "S"
            selected_data_masks.append(tree_search_level_mask)

            # Compute additional terms for training
            if self.training:
                # Compute Entropy Bonus Entropy Bonus
                entropy_att_scores_list.append(
                    (-search_att_weight * torch.log(search_att_weight + 1e-9)).sum(-1)
                ) 
                # Compute action log probabilities --> TODO: check here!
                log_branch_sel_prob = torch.log(level_search_att_weight_mean_nodes.squeeze(1)[
                        torch.arange(B, device="cuda"), selected_indices.flatten()
                    ].squeeze(-1))
                print("lbsp", log_branch_sel_prob.shape)
                log_branch_sel_prob_list.append(log_branch_sel_prob)

        # Aggregate the selected nodes
        search_data_embeddings = torch.cat(selected_data_embeddings, dim=1)
        search_data_masks = torch.cat(selected_data_masks, dim=1).transpose(1, 2)
        
        # Using the aggregated nodes, compute the final embedding
        # pred_emb_pre_out: [B*M, 1, D], flattened_query_Data:[B*M, 1, D], search_data_embeddings: [B*M, N_i, D], search_masks: [B*M, 1, N_i]
        print("sde:", search_data_embeddings.shape)
        print("sdm:", search_data_masks.shape)

        # TODO: check shapes within attention
        pred_emb = self.query_model(
            query_data,
            key=search_data_embeddings,
            value=search_data_embeddings,
            src_mask=search_data_masks,
            return_info=False,
        )

        # Reshape the embedding to the correct representation for Attention
        # pred_emb = pred_emb.reshape(B, M, D)

        print("pred emb", pred_emb.shape)
        quit()

        return pred_emb, entropy_att_scores_list, log_branch_sel_prob_list

    def tree_leaves_retrieval(self, query_data):
        M = query_data.shape[1]
        # For computing L_{TCA}
        leaf_data_embeddings, leaf_data_mask = self.train_tree_data[-1]
        leaf_data_embeddings = leaf_data_embeddings.flatten(1, -2)
        leaf_data_mask = leaf_data_mask.flatten(1, -2).transpose(1, 2).repeat(1, M, 1)
        leaf_pred_emb = self.query_model(
            query_data,
            key=leaf_data_embeddings,
            value=leaf_data_embeddings,
            src_mask=leaf_data_mask,
            return_info=False,
        )
        return leaf_pred_emb

    def process_rl_terms(self, entropy_att_scores_list, log_branch_sel_prob_list):
        # For computing L_{RL}
        if len(entropy_att_scores_list) > 0:
            entropy_scores = torch.stack(entropy_att_scores_list, dim=2)
            entropy_scores = entropy_scores.mean()
        else:
            entropy_scores = torch.tensor(0.0).cuda()

        if len(log_branch_sel_prob_list) > 0:
            log_action_probs = torch.stack(log_branch_sel_prob_list, dim=-1)
            log_action_probs = log_action_probs.sum(-1)
        else:
            log_action_probs = torch.tensor(0.0).cuda()
        return entropy_scores, log_action_probs
