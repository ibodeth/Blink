---
name: Memory Management File Persistence
description: Persist user memory facts to a file and keep them synchronized with Blink's internal memory store.
---

# Memory Management File Persistence

This skill handles saving and deleting user facts both in Blink's memory database and in a plain‑text file (`blink_memory.md`).

## Actions
- **save**: Write a `key: value` line to the file and store the fact in memory.
- **delete**: Remove the line containing the key from the file and delete the fact from memory.

## Implementation Steps
1. **Determine action** (`save` or `delete`).
2. **For `save`**:
   - Use a `callback` to run a Python snippet that:
     ```python
     import pathlib, json
     mem_file = pathlib.Path('blink_memory.md')
     key = "{{key}}"
     value = "{{value}}"
     # Update file
     lines = []
     if mem_file.exists():
         lines = mem_file.read_text().splitlines()
     # Remove existing entry for the key
     lines = [l for l in lines if not l.startswith(f"{key}:")]
     lines.append(f"{key}: {value}")
     mem_file.write_text("\n".join(lines) + "\n")
     # Also update Blink's DB via a placeholder (host will handle actual DB update)
     print({"action": "memory", "action_type": "save", "key": key, "value": value})
     ```
3. **For `delete`**:
   - Use a `callback` with Python snippet:
     ```python
     import pathlib
     mem_file = pathlib.Path('blink_memory.md')
     key = "{{key}}"
     if mem_file.exists():
         lines = mem_file.read_text().splitlines()
         lines = [l for l in lines if not l.startswith(f"{key}:")]
         mem_file.write_text("\n".join(lines) + ("\n" if lines else ""))
     print({"action": "memory", "action_type": "delete", "key": key})
     ```
4. **Return a chat response** confirming the operation.

## Usage Example
- To save a favorite color:
  ```json
  {"type": "memory", "action": "save", "key": "favorite_color", "value": "blue", "response": "Saved your favorite color.", "lang": "en"}
  ```
- To delete it:
  ```json
  {"type": "memory", "action": "delete", "key": "favorite_color", "response": "Removed your favorite color.", "lang": "en"}
  ```

The host will execute the appropriate `callback` snippets to keep the file in sync.

---
**Note**: The skill assumes the working directory is the Blink workspace root where `blink_memory.md` resides.
