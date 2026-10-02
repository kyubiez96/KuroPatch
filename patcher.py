import os
import zipfile
import json
import time
from deep_translator import GoogleTranslator

class JarPatcher:
    def __init__(self, jar_path):
        self.jar_path = jar_path
        self.workspace = "jar_workspace"
        if not os.path.exists(self.workspace):
            os.makedirs(self.workspace)

    def extract(self):
        with zipfile.ZipFile(self.jar_path, 'r') as zip_ref:
            zip_ref.extractall(self.workspace)

    def get_properties_files(self):
        files = []
        for root, _, filenames in os.walk(self.workspace):
            for filename in filenames:
                if filename.endswith(".properties"):
                    files.append(os.path.join(root, filename))
        return files

    def read_file(self, path):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.readlines()

    def write_file(self, path, lines):
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

    def rebuild(self, output_name):
        with zipfile.ZipFile(output_name, 'w') as zip_ref:
            for root, _, files in os.walk(self.workspace):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.workspace)
                    zip_ref.write(full_path, rel_path)
        print(f"[*] Done! Saved as {output_name}")
