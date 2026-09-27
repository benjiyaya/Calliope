import os
import sys
import time
import urllib.request
import urllib.error

COMFY_MODELS = r"E:\ComfyUI-Easy-Install\ComfyUI\models"
LOG_FILE = r"E:\Calliope\logs\model_downloads.log"

DOWNLOAD_QUEUE = [
    # --- Batch 1: Essenziali leggeri (LoRA e Frame Interpolation) ---
    {
        "name": "film_net_fp16.safetensors",
        "url": "https://huggingface.co/Comfy-Org/frame_interpolation/resolve/main/frame_interpolation/film_net_fp16.safetensors",
        "dest": os.path.join(COMFY_MODELS, "frame_interpolation", "film_net_fp16.safetensors"),
        "min_size": 60 * 1024 * 1024,
    },
    {
        "name": "h3-realism-people-t2v-i2v-r2v(r34l1sm).safetensors",
        "url": "https://huggingface.co/fal/MiniMax-H3-Realism-People-LoRA/resolve/main/h3-realism-people-t2v-i2v-r2v.safetensors",
        "dest": os.path.join(COMFY_MODELS, "loras", "minimax-h3", "h3-realism-people-t2v-i2v-r2v(r34l1sm).safetensors"),
        "alias": os.path.join(COMFY_MODELS, "loras", "minimax-h3", "h3-realism-people-t2v-i2v-r2v.safetensors"),
        "min_size": 120 * 1024 * 1024,
    },
    {
        "name": "QuadView_krea2_v1.safetensors",
        "url": "https://huggingface.co/Alissonerdx/CharacterSheet/resolve/main/QuadView_krea2_v1.safetensors",
        "dest": os.path.join(COMFY_MODELS, "loras", "krea2", "QuadView_krea2_v1.safetensors"),
        "min_size": 800 * 1024 * 1024,
    },
    {
        "name": "Krea2-realism-V2.safetensors",
        "url": "https://huggingface.co/RudySen/Krea2-realism-V2/resolve/main/Krea2-realism-V2.safetensors",
        "dest": os.path.join(COMFY_MODELS, "loras", "krea2", "Krea2-realism-V2.safetensors"),
        "min_size": 1400 * 1024 * 1024,
    },
    {
        "name": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        "dest": os.path.join(COMFY_MODELS, "loras", "minimax-h3", "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"),
        "alias": os.path.join(COMFY_MODELS, "loras", "minimax-h3", "minimax_h3_fl2v_turbo_4step_v1.1_768p_comfyui_bf16.safetensors"),
        "min_size": 1800 * 1024 * 1024,
    },

    # --- Batch 2: Componenti LTX-2.5 (VAEs e Upscaler) ---
    {
        "name": "ltx-2.5-audio-vae-bf16.safetensors",
        "url": "https://huggingface.co/osantinello/LTX25_Models/resolve/main/ltx-2.5-audio-vae-bf16.safetensors",
        "dest": os.path.join(COMFY_MODELS, "vae", "ltx-2.5", "ltx-2.5-audio-vae-bf16.safetensors"),
        "min_size": 300 * 1024 * 1024,
    },
    {
        "name": "ltx-2.5-video-vae-bf16.safetensors",
        "url": "https://huggingface.co/osantinello/LTX25_Models/resolve/main/ltx-2.5-video-vae-bf16.safetensors",
        "dest": os.path.join(COMFY_MODELS, "vae", "ltx-2.5", "ltx-2.5-video-vae-bf16.safetensors"),
        "min_size": 1200 * 1024 * 1024,
    },
    {
        "name": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
        "url": "https://huggingface.co/osantinello/LTX25_Models/resolve/main/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
        "dest": os.path.join(COMFY_MODELS, "latent_upscale_models", "ltx-2.5", "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"),
        "min_size": 900 * 1024 * 1024,
    },

    # --- Batch 3: Grandi Modelli (Gemma text encoder, LTX transformer, Qwen INT8) ---
    {
        "name": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
        "url": "https://huggingface.co/osantinello/LTX25_Models/resolve/main/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
        "dest": os.path.join(COMFY_MODELS, "text_encoders", "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors"),
        "min_size": 13 * 1024 * 1024 * 1024,
    },
    {
        "name": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
        "url": "https://huggingface.co/osantinello/LTX25_Models/resolve/main/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
        "dest": os.path.join(COMFY_MODELS, "diffusion_models", "ltx-2.5", "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"),
        "min_size": 19 * 1024 * 1024 * 1024,
    },
    {
        "name": "qwen3vl_32b_minimax_h3_int8_convrot.comfy.safetensors",
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
        "dest": os.path.join(COMFY_MODELS, "text_encoders", "minimax-h3", "qwen3vl_32b_minimax_h3_int8_convrot.comfy.safetensors"),
        "alias": os.path.join(COMFY_MODELS, "text_encoders", "minimax-h3", "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"),
        "min_size": 24 * 1024 * 1024 * 1024,
    },
]


def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def download_file(item):
    name = item["name"]
    url = item["url"]
    dest = item["dest"]
    min_size = item.get("min_size", 0)

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.exists(dest) and os.path.getsize(dest) >= min_size:
        size_gb = os.path.getsize(dest) / (1024**3)
        log(f"SKIP: {name} già scaricato ({size_gb:.2f} GB)")
        create_alias(item)
        return True

    temp_file = dest + ".downloading"
    retries = 10

    for attempt in range(1, retries + 1):
        try:
            downloaded = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
                log(f"Ripristino download di {name} da byte {downloaded} ({downloaded/(1024**2):.1f} MB)...")
            else:
                log(f"Inizio download di {name}...")

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_length = resp.headers.get("Content-Length")
                total_size = int(content_length) + downloaded if content_length else None

                mode = "ab" if downloaded > 0 and resp.status == 206 else "wb"
                if mode == "wb":
                    downloaded = 0

                start_time = time.time()
                last_log_time = start_time
                bytes_since_log = 0

                with open(temp_file, mode) as out:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1MB
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        bytes_since_log += len(chunk)

                        now = time.time()
                        if now - last_log_time >= 8.0:
                            elapsed = now - last_log_time
                            speed_mb = (bytes_since_log / (1024**2)) / elapsed
                            pct = f"{(downloaded / total_size * 100):.1f}%" if total_size else "??%"
                            down_mb = downloaded / (1024**2)
                            tot_mb = f"{total_size / (1024**2):.1f}" if total_size else "??"
                            log(f"[{name}] {pct} ({down_mb:.1f} MB / {tot_mb} MB) @ {speed_mb:.2f} MB/s")
                            last_log_time = now
                            bytes_since_log = 0

            # Verifica finale
            final_size = os.path.getsize(temp_file)
            if min_size and final_size < min_size:
                log(f"WARNING: File {name} sembra incompleto ({final_size} bytes < {min_size}). Riprovo...")
                time.sleep(3)
                continue

            if os.path.exists(dest):
                os.remove(dest)
            os.rename(temp_file, dest)
            log(f"COMPLETATO: {name} ({final_size / (1024**2):.1f} MB)")
            create_alias(item)
            return True

        except Exception as e:
            log(f"Tentativo {attempt}/{retries} fallito per {name}: {e}")
            time.sleep(5)

    log(f"ERRORE: Impossibile scaricare {name} dopo {retries} tentativi.")
    return False


def create_alias(item):
    dest = item["dest"]
    alias = item.get("alias")
    if alias and os.path.exists(dest):
        try:
            if not os.path.exists(alias):
                os.makedirs(os.path.dirname(alias), exist_ok=True)
                import shutil
                # Create hard link or copy
                try:
                    os.link(dest, alias)
                    log(f"Alias creato (hard link): {os.path.basename(alias)} -> {os.path.basename(dest)}")
                except Exception:
                    shutil.copy2(dest, alias)
                    log(f"Alias creato (copia): {os.path.basename(alias)} -> {os.path.basename(dest)}")
        except Exception as e:
            log(f"Nota su alias per {item['name']}: {e}")


def main():
    log("=== AVVIO PROCEDURA DOWNLOAD MODELLI COMFYUI ===")
    log(f"Directory modelli di destinazione: {COMFY_MODELS}")
    log(f"Totale modelli in coda: {len(DOWNLOAD_QUEUE)}")

    success_count = 0
    for idx, item in enumerate(DOWNLOAD_QUEUE, 1):
        log(f"\n--- Modello {idx}/{len(DOWNLOAD_QUEUE)}: {item['name']} ---")
        ok = download_file(item)
        if ok:
            success_count += 1
        else:
            log(f"Proseguo con il modello successivo nonostante l'errore su {item['name']}.")

    log(f"\n=== PROCEDURA TERMINATA: {success_count}/{len(DOWNLOAD_QUEUE)} modelli completati ===")


if __name__ == "__main__":
    main()
