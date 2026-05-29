# Programming Style Guide

## Python

**Naming & Types:**
- Functions/variables: `snake_case`, always include type hints
- Classes: `PascalCase`, constants: `UPPER_CASE`
- Comment above each function describing its purpose

**Imports:**
- Order: stdlib → third-party → local
- One import per line

**Spacing:**
- 4 spaces indentation
- 2 blank lines between top-level functions/classes
- 1 blank line between logical sections in functions
- 1 space around operators: `x = 10`, not `result=calculate(threshold=20)`

**Example:**
```python
# Returns sorted files from input directory, optionally filtered by extension.
def iter_input_files(input_dir: Path, ext_filter: str) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    
    files = [p for p in input_dir.iterdir() if p.is_file()]
    if ext_filter:
        files = [p for p in files if p.suffix.lower() == ext_filter.lower()]
    
    return sorted(files)
```

---

## JavaScript

**Naming & Conventions:**
- Variables/functions: `camelCase`, prefer `const` over `let` over `var`
- Constants: `UPPER_CASE`
- Comment above each function describing its purpose
- Use ES6+: arrow functions, template literals, async/await

**Spacing:**
- Tab indentation
- 1 blank line between logical sections
- 1 space around operators and after control keywords: `if (x === 10)`, not `if(x===10)`
- Semicolons required

**Error Handling:**
- Async functions must have try/catch with meaningful error messages

**Example:**
```javascript
// Saves file to IndexedDB with given name and binary content
async function saveFileToIndexedDB(name, arrayBuffer) {
	try {
		const db = await openFilesDB();
		const tx = db.transaction('files', 'readwrite');
		const store = tx.objectStore('files');
		const blob = new Blob([arrayBuffer], { type: 'application/octet-stream' });
		return new Promise((resolve, reject) => {
			const req = store.add({ name, blob });
			req.onsuccess = (ev) => resolve(ev.target.result);
			req.onerror = () => reject(req.error);
		});
	} catch (err) {
		throw new Error(`Failed to save file: ${err.message}`);
	}
}
```

---

## Comments

**Philosophy:** Explain **why**, not **what**. Code should be self-explanatory.

- Python: Use `#` with space
- JavaScript: Use `//` with space  
- Place above code (preferred) or inline
- Always capitalize first letter
- Use `TODO`, `FIXME` for future work

**Function comments:**
```python
# Executes Python module as subprocess and captures output.
def run_module(args: list[str]) -> dict[str, Any]:
```

```javascript
// Opens or creates IndexedDB database for storing binary files.
function openFilesDB() {
```

---

## General Rules

- Max line length: 100 characters (break at logical points)
- Type hints required for all Python functions
- Meaningful variable names (avoid single letters except `i`, `x`, `y`)
- One space around binary operators: `=`, `==`, `+`, `-`, etc.
- No spaces in keyword arguments: `func(threshold=20)` not `func(threshold = 20)`
