#!/usr/bin/env python3
"""
Game String Translator - Core Engine
Decompile APK → Extract strings → Translate → Patch → Rebuild
"""

import os
import sys
import json
import subprocess
import tempfile
import shutil
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Try to import progress bar
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    tqdm = lambda x, **kwargs: x  # fallback

@dataclass
class TranslationJob:
    """Individual string translation task"""
    key: str
    original: str
    translated: str = ""
    status: str = "pending"  # pending, queued, processing, done, error
    error: str = ""
    retry_count: int = 0

@dataclass
class TranslationQueue:
    """Batch translation queue with progress tracking"""
    items: List[TranslationJob] = field(default_factory=list)
    completed: int = 0
    total: int = 0
    in_progress: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    progress_callback: Optional[Callable] = None
    
    def add(self, job: TranslationJob):
        with self.lock:
            self.items.append(job)
            self.total = len(self.items)
    
    def mark_done(self, key: str, translated: str, error: str = None):
        with self.lock:
            for job in self.items:
                if job.key == key:
                    job.translated = translated
                    job.status = "done" if not error else "error"
                    job.error = error or ""
                    break
            self.completed += 1
            self.in_progress = max(0, self.in_progress - 1)
            if self.progress_callback:
                self.progress_callback(self.completed, self.total)
    
    def mark_start(self, key: str):
        with self.lock:
            self.in_progress += 1
            if self.progress_callback:
                self.progress_callback(self.completed, self.total)
    
    def get_stats(self) -> Dict:
        with self.lock:
            return {
                "completed": self.completed,
                "total": self.total,
                "in_progress": self.in_progress,
                "pending": self.total - self.completed - self.in_progress,
                "items": self.items
            }

class GameTranslator:
    """Main translator engine for Android games"""
    
    def __init__(self, output_dir: str = None):
        self.workspace = tempfile.mkdtemp(prefix="game_translator_")
        self.output_dir = output_dir or self.workspace
        self.tools = {
            "apktool": None,
            "jadx": None,
            "apksigner": None,
            "zipalign": None
        }
        self.keystore_path = None
        self.keystore_pass = "android"  # default debug keystore
        self.alias = "key0"
        self.key_pass = "android"
        
        # Translation config
        self.target_lang = "id"  # default Indonesian
        self.api_key = os.getenv("TRANSLATE_API_KEY", "")
        self.batch_size = 50
        self.max_retries = 3
        
        # Progress tracking
        self.queue = TranslationQueue()
        self.callbacks = {
            "status": None,
            "progress": None,
            "log": None
        }
    
    def set_callbacks(self, **kwargs):
        """Register progress/status callbacks"""
        self.callbacks.update(kwargs)
        if "progress" in kwargs:
            self.queue.progress_callback = kwargs["progress"]
    
    def log(self, message: str):
        """Log message to callback or stdout"""
        if self.callbacks.get("log"):
            self.callbacks["log"](message)
        else:
            print(f"[LOG] {message}")
    
    def set_status(self, status: str, details: str = ""):
        """Update status callback"""
        if self.callbacks.get("status"):
            self.callbacks["status"](status, details)
    
    def setup_tools(self, tools_dir: str) -> bool:
        """Verify and setup required tools"""
        self.log(f"Setting up tools in {tools_dir}")
        
        for tool in ["apktool", "jadx", "apksigner", "zipalign"]:
            path = os.path.join(tools_dir, tool)
            if os.path.exists(path):
                self.tools[tool] = path
                self.log(f"  ✓ {tool} found")
            else:
                self.log(f"  ✗ {tool} not found at {path}")
                return False
        
        return True
    
    def download_tools(self, tools_dir: str, callback=None) -> Dict[str, bool]:
        """Download required tools if not present"""
        results = {}
        
        self.log("Checking for required tools...")
        
        # Tool URLs (from official releases)
        tool_urls = {
            "apktool": "https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar",
            "jadx": "https://github.com/skywinder/jadx/releases/download/v1.5.0/jadx-1.5.0.zip",
            "apksigner": None,  # Part of Android SDK
            "zipalign": None    # Part of Android SDK
        }
        
        for tool, url in tool_urls.items():
            tool_path = os.path.join(tools_dir, tool)
            
            if os.path.exists(tool_path):
                results[tool] = True
                self.log(f"  ✓ {tool} already present")
                continue
            
            if url:
                self.log(f"  Downloading {tool}...")
                if callback:
                    callback(f"Downloading {tool}...", 0)
                
                # Download logic here (simplified)
                # In real implementation, use requests or urllib
                results[tool] = False  # Placeholder
                self.log(f"  ✗ {tool} download failed (placeholder)")
            else:
                # SDK tools - check system
                sdk_path = os.environ.get("ANDROID_SDK", "")
                if sdk_path:
                    possible_paths = [
                        os.path.join(sdk_path, "build-tools", tool),
                        tool  # system path
                    ]
                    for pp in possible_paths:
                        if shutil.which(pp):
                            self.tools[tool] = pp
                            results[tool] = True
                            self.log(f"  ✓ {tool} found in SDK")
                            break
                    else:
                        results[tool] = False
                        self.log(f"  ✗ {tool} not in SDK")
                else:
                    results[tool] = False
                    self.log(f"  ✗ {tool} not found")
        
        return results
    
    def decompile_apk(self, apk_path: str) -> bool:
        """Decompile APK to workspace"""
        self.set_status("decompiling", "Extracting APK resources...")
        self.log(f"Decompiling: {apk_path}")
        
        output_dir = os.path.join(self.workspace, "decompiled")
        
        try:
            # Check if apktool is available
            if not self.tools.get("apktool"):
                self.log("Error: apktool not found")
                return False
            
            # Run apktool d
            cmd = [
                "java", "-jar", self.tools["apktool"],
                "d", "-f", "-r",  # force, no resources
                apk_path,
                output_dir
            ]
            
            self.log(f"Running: {' '.join(cmd)}")
            
            # Execute (simplified - in real app, handle subprocess properly)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                self.log(f"apktool error: {result.stderr}")
                return False
            
            self.log(f"✓ Decompiled to {output_dir}")
            return True
            
        except Exception as e:
            self.log(f"Error decompiling: {e}")
            return False
    
    def extract_strings(self) -> Dict[str, str]:
        """Extract translatable strings from decompiled APK"""
        self.set_status("extracting", "Scanning for strings...")
        self.log("Extracting translatable strings...")
        
        strings = {}
        decompiled_dir = os.path.join(self.workspace, "decompiled")
        
        # Method 1: Extract from resources/strings.xml
        xml_path = os.path.join(decompiled_dir, "res", "values", "strings.xml")
        if os.path.exists(xml_path):
            self.log(f"Found strings.xml: {xml_path}")
            strings.update(self._parse_xml_strings(xml_path))
        
        # Method 2: Extract from smali files
        smali_dir = os.path.join(decompiled_dir, "smali")
        if os.path.exists(smali_dir):
            self.log(f"Scanning smali directory...")
            strings.update(self._extract_from_smali(smali_dir))
        
        self.log(f"✓ Found {len(strings)} translatable strings")
        return strings
    
    def _parse_xml_strings(self, xml_path: str) -> Dict[str, str]:
        """Parse strings.xml file"""
        strings = {}
        
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            for elem in root:
                if elem.tag == "string":
                    key = elem.get("name", "")
                    value = elem.text or ""
                    if key and value:
                        strings[key] = value
                        self.queue.add(TranslationJob(key=key, original=value))
        
        except Exception as e:
            self.log(f"Error parsing XML: {e}")
        
        return strings
    
    def _extract_from_smali(self, smali_dir: str) -> Dict[str, str]:
        """Extract string literals from smali files"""
        strings = {}
        
        try:
            for root, dirs, files in os.walk(smali_dir):
                for filename in files:
                    if filename.endswith(".smali"):
                        filepath = os.path.join(root, filename)
                        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            # Find string literals (simplified)
                            import re
                            matches = re.findall(r'"([^"]+)"', content)
                            for match in matches:
                                if len(match) > 2 and len(match) < 500:  # reasonable length
                                    # Generate key from file and position
                                    key = f"{filename}_{hashlib.md5(match.encode()).hexdigest()[:8]}"
                                    if key not in strings:
                                        strings[key] = match
                                        self.queue.add(TranslationJob(key=key, original=match))
        
        except Exception as e:
            self.log(f"Error extracting from smali: {e}")
        
        return strings
    
    def translate_strings(self, strings: Dict[str, str], source_lang: str = "en") -> Dict[str, str]:
        """Translate strings using API or local model"""
        self.set_status("translating", f"Translating {len(strings)} strings...")
        self.log(f"Starting translation: {len(strings)} strings → {self.target_lang}")
        
        translated = {}
        
        # Process in batches
        items = list(strings.items())
        total = len(items)
        
        for i in tqdm(range(0, total, self.batch_size), desc="Translating batches") if HAS_TQDM else range(0, total, self.batch_size):
            batch = items[i:i + self.batch_size]
            batch_keys = [k for k, v in batch]
            
            # Mark as in progress
            for key in batch_keys:
                self.queue.mark_start(key)
            
            # Translate batch (simplified - in real app, call API)
            try:
                results = self._translate_batch(batch, source_lang)
                for key, orig in batch:
                    if key in results:
                        translated[key] = results[key]
                        self.queue.mark_done(key, results[key])
                    else:
                        translated[key] = orig  # keep original if translation fails
                        self.queue.mark_done(key, orig, error="translation failed")
            except Exception as e:
                self.log(f"Batch translation error: {e}")
                for key, orig in batch:
                    translated[key] = orig
                    self.queue.mark_done(key, orig, error=str(e))
        
        self.log(f"✓ Translated {len(translated)} strings")
        return translated
    
    def _translate_batch(self, batch: List[tuple], source_lang: str) -> Dict[str, str]:
        """Translate a batch of strings"""
        results = {}
        
        # Placeholder for actual translation API
        # In real implementation, call Google Translate, DeepL, or local model
        
        for key, original in batch:
            # Simulate translation (replace with real API call)
            if self.api_key:
                # Real API call here
                # results[key] = self._call_translate_api(original, source_lang, self.target_lang)
                results[key] = f"[{self.target_lang}] {original}"  # placeholder
            else:
                # No API key - just mark for manual translation
                results[key] = original
        
        return results
    
    def _call_translate_api(self, text: str, source: str, target: str) -> str:
        """Call translation API (placeholder)"""
        # Implement actual API call here
        # Examples: Google Translate, DeepL, LibreTranslate
        pass
    
    def patch_strings(self, original_strings: Dict[str, str], translated: Dict[str, str]) -> bool:
        """Patch translated strings back into decompiled APK"""
        self.set_status("patching", "Writing translated strings...")
        self.log("Patching translated strings into APK...")
        
        decompiled_dir = os.path.join(self.workspace, "decompiled")
        
        # Update strings.xml
        xml_path = os.path.join(decompiled_dir, "res", "values", "strings.xml")
        if os.path.exists(xml_path):
            self._write_xml_strings(xml_path, translated)
        
        # Update other language directories if present
        values_dirs = Path(decompiled_dir) / "res" / "values"
        if values_dirs.exists():
            for lang_dir in values_dirs.iterdir():
                if lang_dir.name.startswith("values-") or lang_dir.name == "values":
                    lang_strings = os.path.join(lang_dir, "strings.xml")
                    if os.path.exists(lang_strings):
                        self._write_xml_strings(lang_strings, translated)
        
        self.log("✓ Strings patched successfully")
        return True
    
    def _write_xml_strings(self, xml_path: str, strings: Dict[str, str]):
        """Write translated strings back to XML"""
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            for elem in root:
                if elem.tag == "string":
                    key = elem.get("name", "")
                    if key in strings:
                        elem.text = strings[key]
            
            tree.write(xml_path, encoding='utf-8', xml_declaration=True)
        
        except Exception as e:
            self.log(f"Error writing XML: {e}")
    
    def rebuild_apk(self, input_apk: str) -> str:
        """Rebuild APK from decompiled sources"""
        self.set_status("rebuilding", "Rebuilding APK...")
        self.log("Rebuilding APK...")
        
        decompiled_dir = os.path.join(self.workspace, "decompiled")
        output_apk = os.path.join(self.output_dir, "patched.apk")
        
        try:
            # Run apktool b
            cmd = [
                "java", "-jar", self.tools["apktool"],
                "b",
                decompiled_dir,
                "-o", output_apk
            ]
            
            self.log(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                self.log(f"Build error: {result.stderr}")
                return ""
            
            self.log(f"✓ Rebuilt APK: {output_apk}")
            return output_apk
            
        except Exception as e:
            self.log(f"Error rebuilding: {e}")
            return ""
    
    def sign_apk(self, apk_path: str) -> str:
        """Sign rebuilt APK"""
        self.set_status("signing", "Signing APK...")
        self.log(f"Signing: {apk_path}")
        
        # Create debug keystore if not exists
        keystore_path = os.path.join(self.output_dir, "debug.keystore")
        if not os.path.exists(keystore_path):
            self._create_debug_keystore(keystore_path)
        
        # Zipalign
        aligned_path = os.path.join(self.output_dir, "patched_aligned.apk")
        if self.tools.get("zipalign"):
            cmd = [
                self.tools["zipalign"], "-f", "-v", "4",
                apk_path, aligned_path
            ]
            subprocess.run(cmd, capture_output=True, timeout=60)
            apk_path = aligned_path
        
        # Sign with apksigner
        if self.tools.get("apksigner"):
            cmd = [
                self.tools["apksigner"], "sign",
                "--ks", keystore_path,
                "--ks-pass", f"pass:{self.keystore_pass}",
                "--key-pass", f"pass:{self.key_pass}",
                "--ks-key-alias", self.alias,
                "--out", os.path.join(self.output_dir, "patched_signed.apk"),
                apk_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                self.log("✓ APK signed successfully")
                return os.path.join(self.output_dir, "patched_signed.apk")
            else:
                self.log(f"Sign error: {result.stderr}")
        
        # Fallback: sign with jarsigner
        cmd = [
            "jarsigner", "-verbose", "-sigalg", "SHA1withRSA",
            "-digestalg", "SHA1",
            "-keystore", keystore_path,
            "-storepass", self.keystore_pass,
            "-keypass", self.key_pass,
            apk_path, self.alias
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            self.log("✓ APK signed (jarsigner)")
            return apk_path
        else:
            self.log(f"Sign error: {result.stderr}")
            return ""
    
    def _create_debug_keystore(self, keystore_path: str):
        """Create debug keystore"""
        import shutil
        # Try to copy from Android SDK
        sdk_path = os.environ.get("ANDROID_SDK", "")
        if sdk_path:
            default_keystore = os.path.join(sdk_path, "tools", "lib", "dbgutil", "debug.keystore")
            if os.path.exists(default_keystore):
                shutil.copy2(default_keystore, keystore_path)
                self.log(f"✓ Created debug keystore from SDK")
                return
        
        # Generate new keystore
        cmd = [
            "keytool", "-genkeypair", "-v",
            "-keystore", keystore_path,
            "-storepass", self.keystore_pass,
            "-keypass", self.key_pass,
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "10000",
            "-alias", self.alias,
            "-dname", "CN=Android Debug,O=Android,C=US"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            self.log(f"✓ Generated debug keystore at {keystore_path}")
        else:
            self.log(f"⚠ Keystore generation failed: {result.stderr}")
    
    def clean_workspace(self):
        """Clean up workspace"""
        if os.path.exists(self.workspace):
            shutil.rmtree(self.workspace)
            self.log("✓ Cleaned workspace")
    
    def get_queue_stats(self) -> Dict:
        """Get current queue statistics"""
        return self.queue.get_stats()
    
    def run_full_pipeline(self, apk_path: str, output_dir: str = None) -> Dict:
        """Run complete translation pipeline"""
        if output_dir:
            self.output_dir = output_dir
        
        results = {
            "success": False,
            "output_apk": "",
            "stats": {},
            "errors": []
        }
        
        try:
            # Step 1: Decompile
            if not self.decompile_apk(apk_path):
                results["errors"].append("Failed to decompile APK")
                return results
            
            # Step 2: Extract strings
            original_strings = self.extract_strings()
            if not original_strings:
                results["errors"].append("No translatable strings found")
                return results
            
            # Step 3: Translate
            translated = self.translate_strings(original_strings)
            
            # Step 4: Patch
            if not self.patch_strings(original_strings, translated):
                results["errors"].append("Failed to patch strings")
                return results
            
            # Step 5: Rebuild
            rebuilt = self.rebuild_apk(apk_path)
            if not rebuilt:
                results["errors"].append("Failed to rebuild APK")
                return results
            
            # Step 6: Sign
            signed = self.sign_apk(rebuilt)
            if not signed:
                results["errors"].append("Failed to sign APK")
                return results
            
            results["success"] = True
            results["output_apk"] = signed
            results["stats"] = self.get_queue_stats()
            
            self.log(f"✓ Pipeline complete: {signed}")
            
        except Exception as e:
            results["errors"].append(str(e))
            self.log(f"Pipeline error: {e}")
        
        return results
