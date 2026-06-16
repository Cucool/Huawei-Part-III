def caesar_cipher(text, shift):
    result = ""
    for char in text:
        if char.isalpha():
            # Determine if character is uppercase or lowercase
            start = ord('A') if char.isupper() else ord('a')
            # Shift character and wrap around the alphabet
            result += chr((ord(char) - start + shift) % 26 + start)
        else:
            # Non-alphabetical characters remain unchanged
            result += char
    return result


import random

def split_text_random(text):
    # List of possible splitters
    splitters = ["*", "-", "**", "-*"]

    # Randomly choose one splitter with equal probability (1/4 each)
    splitter = random.choice(splitters)

    # Join each character in the text with the chosen splitter
    result = splitter.join(text)

    return result, splitter  # Return both result and which splitter was used

import base64

def encode_base64(text):
    # Convert the string into bytes
    text_bytes = text.encode('utf-8')
    # Encode bytes to Base64
    encoded_bytes = base64.b64encode(text_bytes)
    # Convert the encoded bytes back to a string
    encoded_str = encoded_bytes.decode('utf-8')
    return encoded_str