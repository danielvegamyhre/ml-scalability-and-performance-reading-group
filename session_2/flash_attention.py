import torch
import triton
import triton.language as tl

class FlashAttention(torch.autograd.Function):
  @staticmethod
  def forward(ctx, Q: torch.Tensor, K: torch.Tensor, V: torch.Tensor, softmax_scale: float):
    HEAD_DIM_Q, HEAD_DIM_K, HEAD_DIM_V = Q.shape[-1], K.shape[-1], V.shape[-1]
    assert HEAD_DIM_Q == HEAD_DIM_K and HEAD_DIM_K == HEAD_DIM_V

    batch_size, num_heads, seq_len, head_dim = Q.shape
    O = torch.empty_like(Q)
    M = torch.zeros((batch_size, num_heads, seq_len))

    #   Parallel kernel instances will each handle a separate
    #   (query block index, head index, index in batch). 
    #   This parallelizes over the Q blocks (the outer for-loop in
    #   the Flash Attention algorithm), and within those blocks
    #   parallelizes further across each sequence (index in the batch
    #   dimension), and within those parallelizes further across each
    #   head.
    #   Total degree of parallelization will be: 
    #   (SEQ_LEN // BLOCK_SIZE Q) * BATCH_SIZE * NUM_HEADS
    grid = lambda meta: (
        triton.cdiv(seq_len, meta["BLOCK_SIZE_Q"]),
        batch_size * num_heads,
        1
    )

    _attn_fwd[grid](
      Q_ptr=Q,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      K_ptr=K,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      V_ptr=V,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      O_ptr=O,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      M_ptr=M,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN
      softmax_scale=softmax_scale,
      stride_Q_batch=Q.stride(0),
      stride_Q_head=Q.stride(1),
      stride_Q_seq=Q.stride(2),
      stride_Q_dim=Q.stride(3),
      stride_K_batch=K.stride(0),
      stride_K_head=K.stride(1),
      stride_K_seq=K.stride(2),
      stride_K_dim=K.stride(3),
      stride_V_batch=V.stride(0),
      stride_V_head=V.stride(1),
      stride_V_seq=V.stride(2),
      stride_V_dim=V.stride(3),
      stride_O_batch=O.stride(0),
      stride_O_head=O.stride(1),
      stride_O_seq=O.stride(2),
      stride_O_dim=O.stride(3),
      stride_M_batch=M.stride(0),
      stride_M_head=M.stride(1),
      stride_M_seq=M.stride(2),
      BATCH_SIZE=batch_size,
      NUM_HEADS=num_heads,
      SEQ_LEN=seq_len,
      HEAD_DIM=head_dim,
    )
    ctx.save_for_backward(Q, K, V, O, M)
    ctx.grid = grid
    ctx.softmax_scale = softmax_scale
    return O
  
  @staticmethod
  def backward(ctx, dO):
    (
      Q, # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      K, # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      V, # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      O, # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
      M, # BATCH_SIZE, NUM_HEADS, SEQ_LEN
    ) = ctx.saved_tensors

    assert dO.is_contiguous()
    assert Q.stride() == K.stride() == V.stride() == O.stride() == dO.stride()

    dQ = torch.empty_like(Q)
    dK = torch.empty_like(K)
    dV = torch.empty_like(V)
    D = torch.empty_like(M)     # intermediate value used to make computing dK dV easier

    BATCH_SIZE, NUM_HEADS, SEQ_LEN = Q.shape[:3]
    HEAD_DIM_KV = Q.shape(3)

    # compute D, the 
    d_grid = lambda meta: (
       triton.cdiv(SEQ_LEN, meta['BLOCK_SIZE_Q']),
       BATCH_SIZE * NUM_HEADS,
    )
    _attn_bwd_preprocess[d_grid](
       O=O,
       dO=dO,
       D=D,
       stride_O_batch=O.stride(0),
       stride_O_head=O.stride(1),
       stride_O_seq=O.stride(2),
       stride_O_dim=O.stride(3),
       stride_dO_batch=dO.stride(0),
       stride_dO_head=dO.stride(1),
       stride_dO_seq=dO.stride(2),
       stride_dO_dim=dO.stride(3),
       stride_D_batch=D.stride(0),
       stride_D_head=D.stride(1),
       stride_D_seq=D.stride(2),
       NUM_HEADS=NUM_HEADS,
       SEQ_LEN=SEQ_LEN,
       HEAD_DIM=HEAD_DIM_KV,
    )

    grid = lambda meta: (
      triton.cdiv(SEQ_LEN, 'BLOCK_SIZE_SEQ'),
      BATCH_SIZE * NUM_HEADS,
    )
    _attn_bwd_dk_dv[grid](
        Q=Q,
        K=K,
        V=V,
        softmax_scale=ctx.softmax_scale,
        dO=dO,
        dQ=dQ,
        dK=dK,
        dV=dV,
        M=M,
        D=D,
        stride_batch=Q.stride(0),
        stride_head=Q.stride(1),
        stride_seq=Q.stride(2),
        stride_dim=Q.stride(3),
        NUM_HEADS=NUM_HEADS,
        SEQ_LEN=SEQ_LEN,
        HEAD_DIM=ctx.head_dim,
    )
    # TODO

@triton.jit
def _attn_bwd_dk_dv(
    Q,
    K,
    V,
    softmax_scale,
    dO,
    dQ,
    dK,
    dV,
    M,
    D,
    stride_q_batch,
    stride_q_head,
    stride_q_seq,
    stride_q_dim,
    NUM_HEADS: tl.constexpr,
    SEQ_LEN: tl.constexpr,
    HEAD_DIM: tl.constexpr,
):
    batch_head_idx = tl.program_id(axis=1)
    batch_idx = batch_head_idx // NUM_HEADS
    head_idx = batch_head_idx % NUM_HEADS

    offs_seq = batch_idx * stride_q_batch + head_idx * stride_q_dim
    # TODO


@triton.jit
def _attn_bwd_preprocess(
    O,   # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    dO,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    D,   # BATCH_SIZE, NUM_HEADS, SEQ_LEN
    stride_O_batch,
    stride_O_head,
    stride_O_seq,
    stride_O_dim,
    stride_dO_batch,
    stride_dO_head,
    stride_dO_seq,
    stride_dO_dim, 
    stride_D_batch,
    stride_D_head,
    stride_D_seq,
    NUM_HEADS: tl.constexpr,
    SEQ_LEN: tl.constexpr,
    HEAD_DIM: tl.constexpr,
    BLOCK_SIZE_SEQ: tl.constexpr,
):
    query_block_idx = tl.program_id(axis=0)
    batch_head_idx = tl.program_id(axis=1)
    batch_idx = batch_head_idx // NUM_HEADS
    head_idx = batch_head_idx % NUM_HEADS

    # load O and dO blocks into SRAM
    offs_seq = query_block_idx * BLOCK_SIZE_SEQ + tl.arange(0, BLOCK_SIZE_SEQ)
    offs_head = tl.arange(0, HEAD_DIM)
    offs_o = (
       batch_idx * stride_O_batch + head_idx * stride_O_head
       + offs_seq[:, None] * stride_O_seq
       + offs_head[None, :]
    )

    O_block = tl.load(offs_o)                            # (BLOCK_SIZE_SEQ, HEAD_DIM)
    dO_block = tl.load(offs_o)                           # (BLOCK_SIZE_SEQ, HEAD_DIM)

    # compute D_i block and store in HBM
    Di_block = tl.sum(O_block * dO_block, axis=1)        # (BLOCK_SIZE_SEQ,)
    offs_di = (
       batch_idx * stride_D_batch + batch_head_idx * stride_D_head
       + offs_seq
    )
    tl.store(D + offs_di, Di_block)

    
@triton.autotune(configs=[
    triton.Config(kwargs={'BLOCK_SIZE_Q': 64, 'BLOCK_SIZE_KV': 64}, num_warps=4),
    triton.Config(kwargs={'BLOCK_SIZE_Q': 128, 'BLOCK_SIZE_KV': 128}, num_warps=4),
  ],
  key=['BATCH_SIZE'],
)
@triton.jit
def _attn_fwd(
    Q_ptr,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    K_ptr,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    V_ptr,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    O_ptr,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM
    M_ptr,  # BATCH_SIZE, NUM_HEADS, SEQ_LEN
    softmax_scale,
    stride_Q_batch,
    stride_Q_head,
    stride_Q_seq,
    stride_Q_dim,
    stride_K_batch,
    stride_K_head,
    stride_K_seq,
    stride_K_dim,
    stride_V_batch,
    stride_V_head,
    stride_V_seq,
    stride_V_dim,
    stride_O_batch,
    stride_O_head,
    stride_O_seq,
    stride_O_dim,
    stride_M_batch,
    stride_M_head,
    stride_M_seq,
    BATCH_SIZE,
    NUM_HEADS: tl.constexpr,
    SEQ_LEN: tl.constexpr,
    HEAD_DIM: tl.constexpr,
    BLOCK_SIZE_Q: tl.constexpr,
    BLOCK_SIZE_KV: tl.constexpr,
):
  '''
  Parallel kernel instances will each handle a separate
  (query block index, head index, index in batch). 
  
  This parallelizes over the Q blocks (the outer for-loop in
  the Flash Attention algorithm), and within those blocks
  parallelizes further across each sequence (index in the batch
  dimension), and within those parallelizes further across each
  head.

  Total degree of parallelization will be: 
  
  (SEQ_LEN // BLOCK_SIZE Q) * BATCH_SIZE * NUM_HEADS

  :Q_ptr: pointer to query tensor
  :K_ptr: pionter to key tensor
  :V_ptr: pointer to value tensor
  :O_ptr: pointer to output tensor to write result to
  :M_ptr: pointer to tensor to store `rowmax[i] + log(softmax_denom[i])`
          values to use to recompute the softmax values in in the backward
          pass with the logsumexp trick
  '''
  inf = 1.0e6

  # each program handles a specific query head for a specific index in the batch dimension.
  # this is represented as a 2D index (query_idx, batch_idx * head_idx)
  query_block_idx = tl.program_id(axis=0)
  batch_head_idx = tl.program_id(axis=1)

  # decompose batch_idx * head_idx into separate batch_idx and head_idx
  batch_idx = batch_head_idx // NUM_HEADS
  head_idx = batch_head_idx % NUM_HEADS

  # calculate offset to this batch and head
  qkv_offset = batch_idx * stride_Q_batch + head_idx * stride_Q_head

  # get subset of Q blocks we are processing in this program id.
  # Q[batch_idx, head_idx, :, :]
  Q_block_ptr = tl.make_block_ptr(
      # by adding the offset to the right batch idx & head idx, 
      # the base points to the start of a tensor of shape (seq, head_dim)
      # within the parent tensor of shape (batch, heads, seq, dim)
      base=Q_ptr + qkv_offset,    # Q[batch_idx, head_idx, :, :]
      shape=(SEQ_LEN, HEAD_DIM),
      strides=(stride_Q_seq, stride_Q_dim),
      # the (seq, head) sub tensor has all the queries in it,
      # so offset into the specific query block we want.
      # # Q[batch_idx, head_idx, q_idx:q_idx+block_size_q, :]
      block_shape=(BLOCK_SIZE_Q, HEAD_DIM),
      offsets=(query_block_idx * BLOCK_SIZE_Q, 0), 
      order=(1,0),
  )

  # get K block. needs to be transposed for Q @ K^T.
  # K[batch_idx, head_idx, :, :]
  K_block_ptr = tl.make_block_ptr(
      base=K_ptr + qkv_offset,
      # inverse shape and stride params to transpose w.r.t. Q
      shape=(HEAD_DIM, SEQ_LEN),
      strides=(stride_K_dim, stride_K_seq),
      # for K,V we select all keys and values, not a sub-block like in Q,
      # so we don't add any offsets and just start at the beginning of the block.
      offsets=(0, 0),
      block_shape=(HEAD_DIM, BLOCK_SIZE_KV),
      order=(1,0),
  )

  # get V block.
  # V[batch_idx, head_idx, :, :]
  V_block_ptr = tl.make_block_ptr(
      base=V_ptr + qkv_offset,
      shape=(SEQ_LEN, HEAD_DIM),
      strides=(stride_V_seq, stride_V_dim),
      offsets=(0, 0),
      block_shape=(BLOCK_SIZE_KV, HEAD_DIM),
      order=(1,0),
  )

  # get O (output) block ptrs.
  O_block_ptr = tl.make_block_ptr(
      # points to O[batch_idx, head_idx, :, :] 
      # of shape (seq, head dim) just like Q,K,V.
      base=O_ptr + qkv_offset,
      shape=(SEQ_LEN, HEAD_DIM),
      strides=(stride_O_seq, stride_O_dim),
      # offsets will be same as Q since we are writing
      # outputs for the subset of queries process in this program id.
      offsets=(query_block_idx * BLOCK_SIZE_Q, 0), 
      block_shape=(BLOCK_SIZE_Q, HEAD_DIM),
      order=(1,0),
  )

  # m_i = max seen so far in QK. track one for each query.
  s_max = tl.full((BLOCK_SIZE_Q,), -float('inf'), dtype=tl.float32)

  # l_i = accumlated global softmax denominator / exp sum
  softmax_denom = tl.zeros((BLOCK_SIZE_Q,), dtype=tl.float32)

  # accumulator for block of output matrix being computed by this program id.
  O_block = tl.zeros((BLOCK_SIZE_Q, HEAD_DIM), dtype=tl.float32)

  # load Q block into SRAM, it will be shared for all iterations of inner
  # loop doing O = softmax(Q @ K^T / scale) @ V
  Q_block = tl.load(Q_block_ptr)                                # (BLOCK_SIZE_Q, HEAD_DIM)

  # set up q block and kv block offsets for causal masking.
  offs_q = query_block_idx * BLOCK_SIZE_Q + tl.arange(0, BLOCK_SIZE_Q)
  offs_kv = tl.arange(0, BLOCK_SIZE_KV)

  # for each Q block, iterate through all associated K and V blocks
  # (up through diagonal of QK, since this is causal attention we don't need to compute
  # values for the top right triangle of QK).
  end_key_idx = (query_block_idx + 1) * BLOCK_SIZE_Q
  for start_kv_idx in tl.range(0, end_key_idx, BLOCK_SIZE_KV):
    causal_mask = offs_q[:, None] >= (start_kv_idx + offs_kv[None, :])

    # load next K block into SRAM
    K_block = tl.load(K_block_ptr)                              # (HEAD_DIM, BLOCK_SIZE_Q)

    # load V block into SRAM
    V_block = tl.load(V_block_ptr)                              # (BLOCK_SIZE_KV, HEAD_DIM)
    
    # compute attention scores
    # S[i,j]
    S_block = (
        tl.dot(Q_block, K_block) 
        * softmax_scale 
        + tl.where(causal_mask, 0, -inf)
    )                                                           # (BLOCK_SIZE_Q, BLOCK_SIZE_KV)

    # m[i,j]
    local_s_max = tl.max(S_block, axis=1)                       # (BLOCK_SIZE_Q,)
    new_s_max = tl.maximum(s_max, local_s_max)

    # corrective factor for previously accumulated denominator
    corrective_factor = tl.exp(s_max - new_s_max)               # (BLOCK_SIZE_Q,)

    # P[i,j] (exp scores)
    P_block = tl.exp(S_block - new_s_max[:, None])              # (BLOCK_SIZE_Q, BLOCK_SIZE_KV)

    # rowsum(P[i,j])
    P_rowsum = tl.sum(P_block, axis=1)                          # (BLOCK_SIZE_Q,)

    # l[i,j]
    softmax_denom = (
        corrective_factor * softmax_denom + P_rowsum            # (BLOCK_SIZE_Q,)
    )

    # apply corrective factor to O block
    # O[i,j]
    O_block = O_block * corrective_factor[:, None]              # (BLOCK_SIZE_Q, HEAD_DIM)
    O_block = O_block + tl.dot(P_block, V_block)                # (BLOCK_SIZE_Q, HEAD_DIM)

    # m[i] -- update global max
    s_max = tl.maximum(s_max, local_s_max)

    # move to next K,V blocks
    K_block_ptr = tl.advance(K_block_ptr, (0, BLOCK_SIZE_KV))
    V_block_ptr = tl.advance(V_block_ptr, (BLOCK_SIZE_KV, 0))

  # normalize scores to finalize softmax block
  O_block = O_block / softmax_denom[:, None]                    # (BLOCK_SIZE_Q, HEAD_DIM)
    
  # store O block output in HBM
  tl.store(O_block_ptr, O_block)

  # store m_i + log(l_i) which can be used to recompute softmax in backward pass
  # using the logsumexp trick.
  s_max += tl.math.log(softmax_denom)                           # (BLOCK_SIZE_Q,)

  offs_m = offs_q + (batch_idx * stride_M_batch) + (head_idx * stride_M_head)
  tl.store(M_ptr + offs_m, s_max)
  

def test_op(BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM, dtype=torch.float32):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    Q = (
        torch.empty(
            (BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM), dtype=dtype, device=device
        )
        .normal_(mean=0.0, std=0.5)
        .requires_grad_()
    )
    K = (
        torch.empty(
            (BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM), dtype=dtype, device=device
        )
        .normal_(mean=0.0, std=0.5)
        .requires_grad_()
    )
    V = (
        torch.empty(
            (BATCH_SIZE, NUM_HEADS, SEQ_LEN, HEAD_DIM), dtype=dtype, device=device
        )
        .normal_(mean=0.0, std=0.5)
        .requires_grad_()
    )

    softmax_scale = 1 / (HEAD_DIM**0.5)

    # reference implementation
    MASK = torch.tril(torch.ones((SEQ_LEN, SEQ_LEN), device=device))
    P = torch.matmul(Q, K.transpose(2, 3)) * softmax_scale
    P[:, :, MASK == 0] = float("-inf")
    P = torch.softmax(P.float(), dim=-1)
    ref_O = torch.matmul(P, V)

    # triton implementation
    flash_out = FlashAttention.apply(Q, K, V, softmax_scale)

    # compare
    rtol = 0.0
    atol = 1e-2
    if not torch.allclose(ref_O, flash_out, atol=atol, rtol=rtol):
        print("want:")
        print(ref_O)
        print("\ngot:")
        print(flash_out)
        print("FAILED")
    else:
        print("PASSED")


if __name__ == "__main__":
    test_op(BATCH_SIZE=8, NUM_HEADS=4, SEQ_LEN=2048, HEAD_DIM=128)