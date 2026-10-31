from PIL import Image

# Открываем нашу картинку
img = Image.open("icon.png")
# Конвертируем в иконку и сохраняем
img.save("icon.ico", format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
print("Иконка icon.ico успешно создана!")