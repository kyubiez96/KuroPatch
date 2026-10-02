import sys
from patcher import JarPatcher
from translator import TranslatorEngine

def translate_lines(lines, engine):
    new_lines = []
    for line in lines:
        if "=" in line:
            parts = line.split("=", 1)
            key = parts[0]
            val = parts[1].strip()
            if len(val) > 3:
                translated = engine.translate(val)
                new_lines.append(f"{key}={translated}\n")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    return new_lines

def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <game.jar>")
        return

    jar_file = sys.argv[1]
    patcher = JarPatcher(jar_file)
    engine = TranslatorEngine()
    
    print("[*] Extracting...")
    patcher.extract()
    
    files = patcher.get_properties_files()
    print(f"[*] Found {len(files)} language files.")
    
    for file in files:
        print(f"[*] Translating {file}...")
        lines = patcher.read_file(file)
        new_lines = translate_lines(lines, engine)
        patcher.write_file(file, new_lines)
        
    patcher.rebuild(jar_file.replace(".jar", "_ID.jar"))

if __name__ == "__main__":
    main()
