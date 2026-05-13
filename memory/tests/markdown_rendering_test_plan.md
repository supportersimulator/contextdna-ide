# Synaptic Chat Markdown Rendering Test Plan

**Target**: Port 8888 Synaptic Chat Interface
**File**: `$HOME/Documents/er-simulator-superrepo/memory/synaptic_chat_server.py`

---

## Current State Analysis

The current implementation in `synaptic_chat_server.py` (lines 376-378) uses an `esc()` function that escapes ALL HTML entities:

```javascript
function esc(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
```

**Problem**: This escapes markdown characters like `*`, `\``, `#`, `|` making them display as literal text instead of rendered markdown.

**Solution Required**: Integrate a markdown parser (like marked.js or showdown.js) to render markdown AFTER escaping dangerous HTML but BEFORE displaying.

---

## Test Messages

Copy and paste these messages into the Synaptic chat to test each markdown feature.

---

### TEST 1: Bold Text

**Send this message:**
```
Testing bold: **this should be bold** and __this too__
```

**Expected Result (with markdown rendering):**
- The words "this should be bold" appear in **bold** formatting
- The words "this too" appear in **bold** formatting

**Expected Result (current - no rendering):**
- Shows literal asterisks: `**this should be bold** and __this too__`

---

### TEST 2: Italic Text

**Send this message:**
```
Testing italic: *this should be italic* and _this too_
```

**Expected Result (with markdown rendering):**
- The words "this should be italic" appear in *italic* formatting
- The words "this too" appear in *italic* formatting

**Expected Result (current - no rendering):**
- Shows literal asterisks/underscores: `*this should be italic* and _this too_`

---

### TEST 3: Inline Code

**Send this message:**
```
Testing inline code: use `console.log()` to debug and `pip install requests` for packages
```

**Expected Result (with markdown rendering):**
- `console.log()` appears in monospace with background highlight
- `pip install requests` appears in monospace with background highlight

**Expected Result (current - no rendering):**
- Shows literal backticks around the code

---

### TEST 4: Code Block with Language

**Send this message:**
```
Here's a Python example:

```python
def hello():
    print("Hello, Synaptic!")
    return True
```

And JavaScript:

```javascript
const greet = () => {
    console.log("Hi from JS");
};
```
```

**Expected Result (with markdown rendering):**
- Python code appears in syntax-highlighted code block
- JavaScript code appears in syntax-highlighted code block
- Proper indentation and monospace font

**Expected Result (current - no rendering):**
- Shows literal triple backticks and language identifiers as text

---

### TEST 5: Unordered List

**Send this message:**
```
Things to remember:
- First item
- Second item
- Third item with **bold**
  - Nested item
  - Another nested
```

**Expected Result (with markdown rendering):**
- Bullet points displayed with proper list formatting
- Indented nested items
- Bold text within list item rendered

**Expected Result (current - no rendering):**
- Shows literal dashes at start of each line

---

### TEST 6: Ordered List

**Send this message:**
```
Steps to follow:
1. First step
2. Second step
3. Third step with `code`
4. Fourth step
```

**Expected Result (with markdown rendering):**
- Numbered list with proper spacing
- Inline code within list item rendered

**Expected Result (current - no rendering):**
- Shows numbers with periods as plain text

---

### TEST 7: Headers

**Send this message:**
```
## Main Section Header

Some content here.

### Subsection Header

More content below.

#### Smaller Header

Final details.
```

**Expected Result (with markdown rendering):**
- `## Main Section Header` appears as large header (h2)
- `### Subsection Header` appears as medium header (h3)
- `#### Smaller Header` appears as smaller header (h4)

**Expected Result (current - no rendering):**
- Shows literal hash symbols: `## Main Section Header`

---

### TEST 8: Tables

**Send this message:**
```
Here's the comparison:

| Feature | Status | Priority |
|---------|--------|----------|
| Bold    | Working | High    |
| Tables  | Testing | Medium  |
| Code    | Pending | High    |
```

**Expected Result (with markdown rendering):**
- Formatted table with borders/lines
- Header row distinguished from data rows
- Columns properly aligned

**Expected Result (current - no rendering):**
- Shows literal pipe characters and dashes as text

---

### TEST 9: Links

**Send this message:**
```
Check the [documentation](https://example.com) and visit [GitHub](https://github.com).
```

**Expected Result (with markdown rendering):**
- "documentation" appears as clickable link
- "GitHub" appears as clickable link
- Links open in new tab when clicked

**Expected Result (current - no rendering):**
- Shows literal brackets and parentheses: `[documentation](https://example.com)`

---

### TEST 10: Combined/Complex Markdown

**Send this message:**
```
## Quick Summary

Here's what we know:

1. **Bold works** with asterisks
2. *Italic works* with single asterisks
3. Code like `npm install` uses backticks

| Test | Result |
|------|--------|
| Pass | ✓      |
| Fail | ✗      |

Check the [docs](https://example.com) for more.

```python
# Example code
print("Synaptic is operational")
```
```

**Expected Result (with markdown rendering):**
- Header rendered as h2
- Ordered list with bold/italic rendered
- Table formatted properly
- Link clickable
- Python code in syntax-highlighted block

---

## Implementation Recommendations

To enable markdown rendering, update the HTML in `synaptic_chat_server.py`:

### Option 1: Use marked.js (Recommended)

Add to the `<head>`:
```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

Update the `addMsg` function:
```javascript
function addMsg(m){
    const t=document.getElementById('thinking');
    if(t)t.remove();
    const div=document.createElement('div');
    div.className=`msg ${m.sender}`;
    const time=m.timestamp?new Date(m.timestamp).toLocaleTimeString():'';

    // Render markdown for message content
    const renderedText = marked.parse(m.message);

    div.innerHTML=`<span class="sender">${m.sender==='aaron'?'👤 AARON':'🧠 SYNAPTIC'}:</span><span class="text">${renderedText}</span><span class="time">${time}</span>`;
    chat.appendChild(div);
}
```

### Option 2: Use showdown.js

Add to the `<head>`:
```html
<script src="https://cdn.jsdelivr.net/npm/showdown/dist/showdown.min.js"></script>
```

Update JavaScript:
```javascript
const converter = new showdown.Converter({tables: true, ghCodeBlocks: true});
// Then use: converter.makeHtml(m.message)
```

### CSS Additions Needed

Add styling for markdown elements:
```css
.text h2, .text h3, .text h4 { margin: 10px 0 5px 0; }
.text h2 { font-size: 1.3em; color: #4a9eff; }
.text h3 { font-size: 1.15em; color: #4a9eff; }
.text code { background: #1a1a1a; padding: 2px 6px; border-radius: 3px; font-family: 'SF Mono', monospace; }
.text pre { background: #1a1a1a; padding: 10px; border-radius: 4px; overflow-x: auto; margin: 10px 0; }
.text pre code { padding: 0; background: transparent; }
.text table { border-collapse: collapse; margin: 10px 0; }
.text th, .text td { border: 1px solid #333; padding: 6px 12px; }
.text th { background: #1a1a1a; }
.text a { color: #4a9eff; text-decoration: underline; }
.text ul, .text ol { margin: 10px 0; padding-left: 20px; }
.text li { margin: 4px 0; }
.text blockquote { border-left: 3px solid #333; padding-left: 10px; color: #888; margin: 10px 0; }
```

---

## Test Execution Checklist

- [ ] Start server: `python memory/synaptic_chat_server.py`
- [ ] Open browser: http://localhost:8888
- [ ] Run TEST 1 (Bold) - Record: Pass/Fail
- [ ] Run TEST 2 (Italic) - Record: Pass/Fail
- [ ] Run TEST 3 (Inline Code) - Record: Pass/Fail
- [ ] Run TEST 4 (Code Block) - Record: Pass/Fail
- [ ] Run TEST 5 (Unordered List) - Record: Pass/Fail
- [ ] Run TEST 6 (Ordered List) - Record: Pass/Fail
- [ ] Run TEST 7 (Headers) - Record: Pass/Fail
- [ ] Run TEST 8 (Tables) - Record: Pass/Fail
- [ ] Run TEST 9 (Links) - Record: Pass/Fail
- [ ] Run TEST 10 (Combined) - Record: Pass/Fail

---

## Security Considerations

When implementing markdown rendering:

1. **XSS Prevention**: Use marked.js with `{sanitize: true}` or DOMPurify to prevent script injection
2. **Link Safety**: Add `rel="noopener noreferrer"` to external links
3. **Content Isolation**: Render in shadow DOM if concerned about style bleeding

Example with DOMPurify:
```javascript
const renderedText = DOMPurify.sanitize(marked.parse(m.message));
```
