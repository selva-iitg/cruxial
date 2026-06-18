# Regenerates the README hero GIF.
#   Render from the repo root:  vhs assets/cruxial-demo.tape
# Shows `cruxial demo` offline: schema blocks → a repair prompt → the action
# layer (receipt → posted, no receipt → UNKNOWN) — the receipt positioning.

Output assets/cruxial-demo.gif

Set Shell "bash"
Set FontSize 19
Set Width 1080
Set Height 1300
Set Padding 24

# Invisible setup: activate the venv + a clean prompt, then clear.
Hide
Type "source .venv/bin/activate" Enter
Type "export PS1='$ '" Enter
Type "clear" Enter
Show

Sleep 600ms
Type "cruxial demo"
Sleep 900ms
Enter
Sleep 5s
