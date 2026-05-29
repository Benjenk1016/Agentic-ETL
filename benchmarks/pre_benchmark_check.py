#!/usr/bin/env python3
"""
Pre-Benchmark Validation Script

Checks that all prerequisites are met before running the benchmark:
- Ollama is running and accessible
- Required Python packages are installed
- Test data exists
- At least one model is available

Run this before running benchmark.py to catch configuration issues early.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def check_title(title):
    """Print a section title."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

def check_ollama():
    """Check if Ollama is running and accessible."""
    print("\n🔍 Checking Ollama connectivity...")
    try:
        import requests
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code == 200:
            print("✅ Ollama is running at http://localhost:11434")
            return True
        else:
            print(f"❌ Ollama returned status code: {response.status_code}")
            return False
    except ImportError:
        print("❌ 'requests' package not installed")
        print("   Install: pip install requests")
        return False
    except Exception as e:
        print(f"❌ Cannot connect to Ollama: {e}")
        print("   Ensure Ollama is installed and running")
        print("   Download: https://ollama.ai/download")
        return False

def check_models():
    """Check if any models are installed."""
    print("\n🔍 Checking installed models...")
    try:
        import requests
        response = requests.get("http://localhost:11434/api/tags", timeout=10)
        if response.status_code == 200:
            data = response.json()
            models = [model["name"] for model in data.get("models", [])]
            if models:
                print(f"✅ Found {len(models)} model(s):")
                for model in models:
                    print(f"   - {model}")
                return True
            else:
                print("❌ No models installed")
                print("   Install at least one model:")
                print("   Example: ollama pull tinyllama:1.1b")
                return False
    except Exception as e:
        print(f"⚠️  Could not check models: {e}")
        return False

def check_python_packages():
    """Check if required Python packages are installed."""
    print("\n🔍 Checking Python packages...")
    required = {
        "requests": "HTTP requests to Ollama API",
        "pandas": "Reading Excel test files",
        "openpyxl": "Excel file support for pandas",
    }
    optional = {
        "psutil": "RAM monitoring during inference",
    }
    
    all_good = True
    for package, purpose in required.items():
        try:
            __import__(package)
            print(f"✅ {package:12} - {purpose}")
        except ImportError:
            print(f"❌ {package:12} - {purpose} (REQUIRED)")
            all_good = False
    
    for package, purpose in optional.items():
        try:
            __import__(package)
            print(f"✅ {package:12} - {purpose} (optional)")
        except ImportError:
            print(f"⚠️  {package:12} - {purpose} (optional, but recommended)")
    
    if not all_good:
        print("\n   Install missing packages:")
        print("   pip install requests pandas openpyxl psutil")
    
    return all_good

def check_test_data():
    """Check if test data exists."""
    print("\n🔍 Checking test data...")
    
    data_dir = PROJECT_ROOT / "data"
    input_dirs = [data_dir / "input", data_dir / "inputs"]
    config_dir = data_dir / "config"
    
    # Find input directory
    input_dir = None
    for candidate in input_dirs:
        if candidate.exists() and candidate.is_dir():
            input_dir = candidate
            break
    
    if not input_dir:
        print("❌ Input directory not found")
        print(f"   Expected: {data_dir / 'input'} or {data_dir / 'inputs'}")
        print("   Create it and add Excel test files")
        return False
    
    # Check for Excel input test files
    test_files = [
        f
        for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".xlsx", ".xls", ".xlsm"}
    ]
    if not test_files:
        print(f"⚠️  Input directory exists but is empty: {input_dir}")
        print("   Add .xlsx/.xls/.xlsm files to test against")
        return False
    
    print(f"✅ Found {len(test_files)} test file(s) in {input_dir.name}/:")
    for f in test_files[:5]:  # Show first 5
        print(f"   - {f.name}")
    if len(test_files) > 5:
        print(f"   ... and {len(test_files) - 5} more")
    
    # Check config directory for Excel configs
    if not config_dir.exists() or not config_dir.is_dir():
        print(f"❌ Config directory not found: {config_dir}")
        print("   Add at least one Excel config file (.xlsx/.xls/.xlsm)")
        return False
    
    config_files = [
        f
        for f in config_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".xlsx", ".xls", ".xlsm"}
    ]
    if not config_files:
        print(f"❌ No Excel config files found in: {config_dir}")
        print("   Add at least one .xlsx/.xls/.xlsm config workbook")
        return False

    print(f"✅ Found {len(config_files)} Excel config file(s) in config/:")
    for f in config_files:
        print(f"   - {f.name}")
    
    return True

def check_system_resources():
    """Check system RAM availability."""
    print("\n🔍 Checking system resources...")
    try:
        import psutil
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)
        total_gb = mem.total / (1024**3)
        percent_used = mem.percent
        
        print(f"   Total RAM: {total_gb:.1f} GB")
        print(f"   Available: {available_gb:.1f} GB ({100-percent_used:.0f}% free)")
        
        if available_gb < 2:
            print("⚠️  Low available RAM (<2GB)")
            print("   Close heavy applications before benchmarking")
            print("   Only small models (tinyllama:1.1b) will work reliably")
        elif available_gb < 4:
            print("⚠️  Moderate RAM (2-4GB available)")
            print("   Recommended: Close Chrome, VS Code, etc.")
            print("   Small models (1B-2B params) should work")
        elif available_gb < 8:
            print("✅ Good RAM availability (4-8GB)")
            print("   Can run models up to ~3B params")
        else:
            print("✅ Excellent RAM availability (>8GB)")
            print("   Can run larger 7B models")
        
        return True
    except ImportError:
        print("⚠️  psutil not installed - cannot check RAM")
        print("   Install: pip install psutil")
        return True  # Not critical for benchmark prep

def main():
    """Run all checks."""
    check_title("Pre-Benchmark Validation")
    
    print("\nThis script validates your setup before running benchmark.py")
    print("It will check:")
    print("  1. Ollama connectivity")
    print("  2. Installed models")
    print("  3. Python packages")
    print("  4. Test data")
    print("  5. System resources")
    
    checks = {
        "Ollama Running": check_ollama(),
        "Models Installed": check_models(),
        "Python Packages": check_python_packages(),
        "Test Data": check_test_data(),
        "System Resources": check_system_resources(),
    }
    
    # Summary
    check_title("Validation Summary")
    
    all_passed = True
    for check_name, passed in checks.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status:8} - {check_name}")
        if not passed:
            all_passed = False
    
    print("\n" + "="*60)
    
    if all_passed:
        print("\n🎉 All checks passed! You're ready to run the benchmark.")
        print("\nRun: python benchmarks/benchmark.py")
    else:
        print("\n❌ Some checks failed. Fix the issues above before running benchmark.")
        print("\nFor help, see:")
        print("  - benchmarks/README.md")
        print("  - benchmarks/QUICK_START.md")
        print("  - LOCAL_OLLAMA_SETUP.md")
    
    print()
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
