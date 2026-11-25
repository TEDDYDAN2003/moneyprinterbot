with open('src/bot/hull/terminal.py', 'r') as f:
    text = f.read()

idx = text.find('summary["history_check"][name] = checks')
print(text[idx-1200:idx-600])
