with open('src/bot/hull/feed.py', 'r') as f:
    text = f.read()

idx = text.find('def rates(')
print(text[idx:idx+400])
