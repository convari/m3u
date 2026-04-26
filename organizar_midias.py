import os
import re
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import requests
import json
from datetime import datetime
from pathlib import Path

# Configurações de Filtro
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm")
MIN_FILE_SIZE_MB = 10

# TMDB API Configuration
TMDB_API_KEY = "4ad74eb1fe80c240834037ea1feded20"
TMDB_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI0YWQ3NGViMWZlODBjMjQwODM0MDM3ZWExZmVkZWQyMCIsIm5iZiI6MTczMjMxOTQ4Ny45MzQsInN1YiI6IjY3NDExOGZmODMzN2FjYWUwNzZkZjQ0ZiIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ._lqY4KcShKvF6kxtal2dhiTyUpBAq85SxoQGW7M6luM"
TMDB_BASE_URL = "https://api.themoviedb.org/3"

class TMDBClient:
    """Cliente para interagir com API TMDB"""
    
    def __init__(self):
        self.headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {TMDB_ACCESS_TOKEN}"
        }
        self.cache = {}
    
    def search_movie(self, title, year=None):
        """Busca filme no TMDB"""
        try:
            query = title.strip()
            params = {
                "query": query,
                "include_adult": False,
                "language": "pt-BR",
                "page": 1
            }
            if year:
                params["primary_release_year"] = year
            
            cache_key = f"movie_{query}_{year}"
            if cache_key in self.cache:
                return self.cache[cache_key]
            
            response = requests.get(
                f"{TMDB_BASE_URL}/search/movie",
                params=params,
                headers=self.headers,
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("results"):
                result = data["results"][0]
                result_data = {
                    "title": result.get("title", query),
                    "year": result.get("release_date", "").split("-")[0] if result.get("release_date") else year,
                    "id": result.get("id"),
                    "original_title": result.get("original_title")
                }
                self.cache[cache_key] = result_data
                return result_data
            
            return None
        except Exception as e:
            print(f"Erro ao buscar filme '{title}': {e}")
            return None
    
    def search_tv(self, title, year=None):
        """Busca série no TMDB"""
        try:
            query = title.strip()
            params = {
                "query": query,
                "include_adult": False,
                "language": "pt-BR",
                "page": 1
            }
            if year:
                params["first_air_date_year"] = year
            
            cache_key = f"tv_{query}_{year}"
            if cache_key in self.cache:
                return self.cache[cache_key]
            
            response = requests.get(
                f"{TMDB_BASE_URL}/search/tv",
                params=params,
                headers=self.headers,
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("results"):
                result = data["results"][0]
                result_data = {
                    "title": result.get("name", query),
                    "year": result.get("first_air_date", "").split("-")[0] if result.get("first_air_date") else year,
                    "id": result.get("id"),
                    "original_name": result.get("original_name")
                }
                self.cache[cache_key] = result_data
                return result_data
            
            return None
        except Exception as e:
            print(f"Erro ao buscar série '{title}': {e}")
            return None

class MediaOrganizer:
    def __init__(self, root):
        self.root = root
        self.root.title("VibeCine - Organizador Jellyfin Pro")
        self.root.geometry("750x550")
        self.folder = tk.StringVar()
        self.tmdb = TMDBClient()
        self.stats = {"processed": 0, "errors": 0}
        self.create_ui()

    def create_ui(self):
        frame = ttk.Frame(self.root, padding=15)
        frame.pack(fill="both", expand=True)

        # Seção de Configuração
        config_frame = ttk.LabelFrame(frame, text="Configuração", padding=10)
        config_frame.pack(fill="x", pady=5)

        ttk.Label(config_frame, text="Diretório da Biblioteca:").pack(anchor="w")
        path_frame = ttk.Frame(config_frame)
        path_frame.pack(fill="x", pady=5)

        ttk.Entry(path_frame, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(path_frame, text="Procurar", command=self.select_folder).pack(side="left", padx=5)

        # Progresso
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=10)

        # Log
        ttk.Label(frame, text="Log de Processamento:").pack(anchor="w", pady=(10, 0))
        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, pady=5)

        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")

        self.log = tk.Text(log_frame, height=15, font=("Consolas", 8), yscrollcommand=scrollbar.set)
        self.log.pack(fill="both", expand=True, side="left")
        scrollbar.config(command=self.log.yview)

        # Botões
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill="x", pady=10)
        ttk.Button(button_frame, text="ORGANIZAR AGORA", command=self.start_thread).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Limpar Log", command=self.clear_log).pack(side="left", padx=5)

    def log_msg(self, msg, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {msg}"
        self.log.insert(tk.END, formatted_msg + "\n")
        self.log.see(tk.END)
        self.root.update_idletasks()

    def clear_log(self):
        self.log.delete(1.0, tk.END)

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder.set(folder)

    def clean_name_strict(self, text):
        """Limpa nome do arquivo removendo lixo"""
        name_part = os.path.splitext(text)[0]
        
        # Remove "mp4" literal que esteja sobrando no nome
        name_part = re.sub(r"\bmp4\b", "", name_part, flags=re.IGNORECASE)
        
        # Remove termos técnicos e qualidades
        name_part = re.sub(
            r"(1080p|720p|480p|4k|8k|uhd|dual|dublado|legendado|h264|h265|x264|x265|r5|web-dl|webrip|bluray|brrip|aac|dd5|dts|10bit|hevc).*",
            "",
            name_part,
            flags=re.IGNORECASE
        )
        
        # Remove caracteres inválidos para Windows/Linux
        name_part = re.sub(r'[<>:"|?*]', "", name_part)
        
        # Substitui pontos, traços e underscores por espaço
        name_part = name_part.replace(".", " ").replace("_", " ").replace("-", " ")
        
        # Remove parênteses vazios
        name_part = re.sub(r"\(\s*\)", "", name_part)
        
        # Limpa espaços extras
        name_part = re.sub(r"\s+", " ", name_part).strip()
        
        return name_part

    def extract_episode_info(self, filename):
        """Extrai informações de episódio (S##E##)"""
        match = re.search(r"S(\d{1,2})E(\d{1,2})", filename, re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
        return None, None

    def extract_year(self, filename):
        """Extrai ano do arquivo"""
        match = re.search(r"\b(19|20)\d{2}\b", filename)
        return match.group() if match else None

    def is_junk(self, file_path):
        """Verifica se arquivo é válido"""
        if not file_path.lower().endswith(VIDEO_EXT):
            return True
        try:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            return size_mb < MIN_FILE_SIZE_MB
        except:
            return True

    def remove_empty_folders(self, base):
        """Remove pastas vazias"""
        for root, dirs, files in os.walk(base, topdown=False):
            if root == base:
                continue
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except:
                pass

    def organize(self):
        """Organiza arquivos de mídia"""
        base = self.folder.get()
        if not os.path.exists(base):
            messagebox.showerror("Erro", "Diretório não existe!")
            return

        self.log_msg("=" * 60)
        self.log_msg("Iniciando organização de mídia...")
        self.log_msg(f"Diretório: {base}")
        self.log_msg("=" * 60)

        valid_files = []
        
        # Coleta arquivos válidos
        self.log_msg("Escaneando arquivos...")
        for root, _, files in os.walk(base):
            for f in files:
                f_path = os.path.join(root, f)
                if not self.is_junk(f_path):
                    valid_files.append(f_path)
                else:
                    try:
                        if not os.path.isdir(f_path):
                            os.remove(f_path)
                            self.log_msg(f"❌ Removido: {f} (tamanho/tipo inválido)")
                    except:
                        pass

        self.log_msg(f"Encontrados {len(valid_files)} arquivo(s) válido(s)")
        self.log_msg("-" * 60)

        # Processa cada arquivo
        for idx, path in enumerate(valid_files, 1):
            file_name = os.path.basename(path)
            ext = os.path.splitext(file_name)[1]
            self.log_msg(f"\n[{idx}/{len(valid_files)}] Processando: {file_name}")

            season, episode = self.extract_episode_info(file_name)
            year = self.extract_year(file_name)

            if season and episode:
                # É uma série
                raw_title = re.split(r"S\d{1,2}E\d{1,2}", file_name, flags=re.IGNORECASE)[0]
                show_name = self.clean_name_strict(raw_title)

                self.log_msg(f"   📺 Identificado como série: {show_name}")
                self.log_msg(f"   🔍 Buscando no TMDB...")

                tmdb_result = self.tmdb.search_tv(show_name, year)
                
                if tmdb_result:
                    final_title = tmdb_result["title"]
                    self.log_msg(f"   ✓ Encontrado: {final_title}")
                else:
                    final_title = show_name
                    self.log_msg(f"   ⚠ Não encontrado no TMDB, usando: {final_title}")

                dest_dir = os.path.join(base, final_title, f"Temporada {season:02d}")
                new_filename = f"{final_title} S{season:02d}E{episode:02d}{ext}"

            else:
                # É um filme
                raw_title = self.clean_name_strict(file_name)
                
                self.log_msg(f"   🎬 Identificado como filme: {raw_title}")
                self.log_msg(f"   🔍 Buscando no TMDB...")

                tmdb_result = self.tmdb.search_movie(raw_title, year)

                if tmdb_result:
                    final_title = tmdb_result["title"]
                    final_year = tmdb_result.get("year", year)
                    self.log_msg(f"   ✓ Encontrado: {final_title} ({final_year})")
                else:
                    final_title = raw_title
                    final_year = year
                    self.log_msg(f"   ⚠ Não encontrado no TMDB, usando: {final_title}")

                if final_year:
                    folder_name = f"{final_title} ({final_year})"
                else:
                    folder_name = final_title

                dest_dir = os.path.join(base, folder_name)
                new_filename = f"{folder_name}{ext}"

            try:
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, new_filename)

                if os.path.abspath(path) != os.path.abspath(dest_path):
                    if not os.path.exists(dest_path):
                        shutil.move(path, dest_path)
                        self.log_msg(f"   ✓ Movido: {new_filename}")
                        self.stats["processed"] += 1
                    else:
                        self.log_msg(f"   ⚠ Já existe: {new_filename}")
                else:
                    self.log_msg(f"   ℹ Já está no local correto")
                    self.stats["processed"] += 1

            except Exception as e:
                self.log_msg(f"   ❌ Erro: {str(e)}")
                self.stats["errors"] += 1

        self.remove_empty_folders(base)
        self.progress.stop()

        self.log_msg("\n" + "=" * 60)
        self.log_msg(f"✓ Organização concluída!")
        self.log_msg(f"📊 Processados: {self.stats['processed']} | Erros: {self.stats['errors']}")
        self.log_msg("=" * 60)

        messagebox.showinfo(
            "VibeCine",
            f"Organização concluída!\n\nProcessados: {self.stats['processed']}\nErros: {self.stats['errors']}"
        )

    def start_thread(self):
        if not self.folder.get():
            messagebox.showwarning("Aviso", "Selecione um diretório!")
            return

        self.stats = {"processed": 0, "errors": 0}
        self.progress.start()
        threading.Thread(target=self.organize, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    MediaOrganizer(root)
    root.mainloop()
