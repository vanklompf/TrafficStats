# TODO

## Optimise frame width for Qwen3-VL

Qwen3-VL's Vision Transformer tiles images into 14×14 pixel grids and rounds
both dimensions to multiples of 28. The model enforces a pixel-count range:

- **min_pixels** = 512 × 28 × 28 ≈ 401k px (~633 × 633)
- **max_pixels** = 2048 × 28 × 28 ≈ 1.6M px (~1267 × 1267)

At the current default of `ANALYSIS_FRAME_WIDTH=512`, frames are 512 × 232 =
~119k pixels — well below `min_pixels`. The model **upscales** them internally
before the ViT processes them, which wastes quality.

### Recommended action

Consider bumping `ANALYSIS_FRAME_WIDTH` to **672** (24 × 28) or **784**
(28 × 28) so frames land closer to the ViT's native input range and avoid
upscale distortion.

### Trade-offs

- Larger frames produce more vision tokens → slower Ollama inference.
- Motion mask must be regenerated to match the new frame dimensions.
- JPEG payload per frame grows (~2× at 784 vs 512 width).

### How to test

Use the test harness to A/B compare widths:

```bash
cd tools
python3 test_video_analysis.py \
  --media-path /path/to/clips \
  --methods motion \
  --motion-mask maska.png \
  --widths 512,672,784 \
  --models qwen3-vl:8b \
  --output width_comparison.json
```

Compare `duration_llm_s`, `ollama_eval_count`, and response quality across
widths.

### References

- [Qwen2.5-VL image preprocessing (GitHub issue #931)](https://github.com/QwenLM/Qwen2.5-VL/issues/931)
- [Qwen2.5-VL preprocessor_config.json](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct/blob/main/preprocessor_config.json)
