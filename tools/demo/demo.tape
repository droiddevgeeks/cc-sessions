# README demo. Fake data only (see make_demo.py). Run from the repo root:
#   vhs tools/demo/demo.tape      -> docs/demo.gif
Output docs/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1440
Set Height 760
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms
Set WindowBar Colorful

Hide
Type `export PS1='$ ' && eval "$(python3 tools/demo/make_demo.py /tmp/ccs-demo --live-pid $$)" && alias ccs="python3 $PWD/cc_sessions.py" && clear`
Enter
Sleep 2s
Show

Type "ccs"
Sleep 300ms
Enter
Sleep 1.5s

# 5 = Pick sessions to delete
Type "5"
Sleep 2s

# tick three old sessions (one hides 3 unpushed commits)
Down
Sleep 400ms
Space
Sleep 400ms
Space
Sleep 400ms
Space
Sleep 800ms
Enter

# it stops and shows the commits that exist nowhere else
Sleep 3.5s
Down
Sleep 600ms
Enter
Sleep 800ms
Type "gateway-config"
Sleep 400ms
Enter
Sleep 1.2s

# scratch only: /resume still works
Enter
Sleep 1s
Type "yes"
Sleep 300ms
Enter
Sleep 4s
