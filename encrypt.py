#! /usr/bin/env python3

import base64
import hmac
import io
import json
import os
import textwrap

import umsgpack
import nacl.bindings
import docopt

__doc__ = '''\
Usage:
    encrypt.py [<message>] [--recipients=<num_recipients>]
               [--chunk=<chunk_size>]
'''

FORMAT_VERSION = 1

# Hardcode the keys for everyone involved.
# ----------------------------------------

jack_private = b'\xaa' * 32
jack_public = nacl.bindings.crypto_scalarmult_base(jack_private)


# Utility functions.
# ------------------

def chunks_with_empty(message, chunk_size):
    'The last chunk is empty, which signifies the end of the message.'
    chunk_start = 0
    chunks = []
    while chunk_start < len(message):
        chunks.append(message[chunk_start:chunk_start+chunk_size])
        chunk_start += chunk_size
    # empty chunk
    chunks.append(b'')
    return chunks


def write_framed_msgpack(stream, obj):
    msgpack_bytes = umsgpack.packb(obj)
    frame = umsgpack.packb(len(msgpack_bytes))
    stream.write(frame)
    stream.write(msgpack_bytes)


def read_framed_msgpack(stream):
    length = umsgpack.unpack(stream)
    print(length)
    # We discard the frame length and stream on.
    obj = umsgpack.unpack(stream)
    print(json_repr(obj))
    return obj


def json_repr(obj):
    # We need to repr everything that JSON doesn't directly support,
    # particularly bytes.
    def _recurse_repr(obj):
        if isinstance(obj, (list, tuple)):
            return [_recurse_repr(x) for x in obj]
        elif isinstance(obj, dict):
            return {_recurse_repr(key): _recurse_repr(val)
                    for key, val in obj.items()}
        elif isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        else:
            return obj
    return json.dumps(_recurse_repr(obj), indent='  ')


# All the important bits!
# -----------------------

def encrypt(sender_private, recipient_groups, message, chunk_size):
    sender_public = nacl.bindings.crypto_scalarmult_base(sender_private)
    encryption_key = os.urandom(32)
    mac_keys = []
    # We will skip MACs entirely if there's only going to be one MAC key. In
    # that case, Box() gives the same guarantees.
    need_macs = (len(recipient_groups) > 1)
    recipients = []
    # First 16 bytes of the recipients nonce is random. The last 8 are the
    # recipient counter.
    recipients_nonce_start = os.urandom(16)
    recipient_num = 0
    for group_num, group in enumerate(recipient_groups):
        if need_macs:
            mac_key = os.urandom(16)
            mac_keys.append(mac_key)
        for recipient in group:
            if need_macs:
                keys = {
                    "encryption_key": encryption_key,
                    "mac_group": group_num,
                    "mac_key": mac_key,
                }
            else:
                keys = {
                    "encryption_key": encryption_key,
                }
            packed_keys = umsgpack.packb(keys)
            recipient_nonce = (recipients_nonce_start +
                               recipient_num.to_bytes(8, byteorder="big"))
            recipient_num += 1
            boxed_keys = nacl.bindings.crypto_box(
                message=packed_keys,
                nonce=recipient_nonce,
                sk=sender_private,
                pk=recipient)
            recipients.append([recipient, boxed_keys])
    header = {
        "version": FORMAT_VERSION,
        "sender": sender_public,
        "nonce": recipients_nonce_start,
        "recipients": recipients,
    }
    output = io.BytesIO()
    write_framed_msgpack(output, header)

    # Write the chunks.
    for chunknum, chunk in enumerate(chunks_with_empty(message, chunk_size)):
        nonce = chunknum.to_bytes(24, byteorder='big')
        # Box and strip the nonce.
        boxed_chunk = nacl.bindings.crypto_secretbox(
            message=chunk,
            nonce=nonce,
            key=encryption_key)
        macs = []
        if need_macs:
            authenticator = boxed_chunk[:16]
            for mac_key in mac_keys:
                hmac_obj = hmac.new(mac_key, digestmod='sha512')
                hmac_obj.update(authenticator)
                macs.append(hmac_obj.digest()[:16])
        chunk_map = {
            "macs": macs,
            "chunk": boxed_chunk,
        }
        write_framed_msgpack(output, chunk_map)

    return output.getvalue()


def decrypt(input, recipient_private):
    stream = io.BytesIO(input)
    # Parse the header.
    header = read_framed_msgpack(stream)
    version = header['version']
    assert version == 1
    sender_public = header['sender']
    recipients_nonce_start = header['nonce']
    recipients = header['recipients']
    # Find this recipient's key box.
    recipient_public = nacl.bindings.crypto_scalarmult_base(recipient_private)
    recipient_num = 0
    for pub, boxed_keys in recipients:
        if pub == recipient_public:
            break
        recipient_num += 1
    else:
        raise RuntimeError('recipient key not found')
    # Unbox the recipient's keys.
    recipient_nonce = (recipients_nonce_start +
                       recipient_num.to_bytes(8, byteorder='big'))
    packed_keys = nacl.bindings.crypto_box_open(
        ciphertext=boxed_keys,
        nonce=recipient_nonce,
        sk=recipient_private,
        pk=sender_public)
    keys = umsgpack.unpackb(packed_keys)
    print(textwrap.indent('keys: ' + json_repr(keys), '### '))
    encryption_key = keys['encryption_key']
    mac_group = keys.get('mac_group')
    mac_key = keys.get('mac_key')
    # Unbox each of the chunks.
    chunknum = 0
    output = io.BytesIO()
    while True:
        nonce = chunknum.to_bytes(24, byteorder='big')
        chunk_map = read_framed_msgpack(stream)
        macs = chunk_map['macs']
        boxed_chunk = chunk_map['chunk']
        # Check the MAC.
        if mac_key is not None:
            their_mac = macs[mac_group]
            authenticator = boxed_chunk[:16]
            hmac_obj = hmac.new(mac_key, digestmod='sha512')
            hmac_obj.update(authenticator)
            our_mac = hmac_obj.digest()[:16]
            if not hmac.compare_digest(their_mac, our_mac):
                raise RuntimeError("MAC mismatch!")
        # Prepend the nonce and decrypt.
        chunk = nacl.bindings.crypto_secretbox_open(
            ciphertext=boxed_chunk,
            nonce=nonce,
            key=encryption_key)
        print('### chunk {}: {}'.format(chunknum, chunk))
        if chunk == b'':
            break
        output.write(chunk)
        chunknum += 1
    return output.getvalue()


def main():
    default_message = b'The Magic Words are Squeamish Ossifrage'
    args = docopt.docopt(__doc__)
    message = args['<message>']
    if message is None:
        encoded_message = default_message
    else:
        encoded_message = message.encode('utf8')
    recipients_len = int(args.get('--recipients') or 1)
    recipients_private = [os.urandom(32) for i in range(recipients_len)]
    recipients_public = [nacl.bindings.crypto_scalarmult_base(r)
                         for r in recipients_private]
    groups = [[p] for p in recipients_public]
    chunk_size = int(args.get('--chunk') or 100)
    output = encrypt(jack_private, groups, encoded_message, chunk_size)
    print(base64.b64encode(output).decode())
    print('-----------------------------------------')
    decoded_message = decrypt(output, recipients_private[0])
    print('message:', decoded_message)


if __name__ == '__main__':
    main()
