/*
Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
SPDX-License-Identifier: LGPL-3.0-only
Hardened 2026: input-length validation, zero-length handling, buffer
release on every exit path, Py_ssize_t counters (2026-08-23).
*/
#define PY_SSIZE_T_CLEAN
#define RLE_HEADER 0xFEFEFEFEFEFEFEFE

#include <Python.h>
#include <math.h>

/* #define (not `const`): a file-scope `const` initialized from another
 * `const` is not a constant expression in C, so MSVC rejects the derived
 * initializers below with C2099 (gcc/clang fold them as an extension).
 * Macros expand to literal constant expressions every compiler accepts —
 * needed so the extension also compiles with MSVC on Windows. Same values. */
#define bt709_coeff_r 0.2126
#define bt709_coeff_g 0.7152
#define bt709_coeff_b 0.0722
#define bt709_coeff_ri (1.0 - bt709_coeff_r)
#define bt709_coeff_bi (1.0 - bt709_coeff_b)
#define bt709_coeff_bg (bt709_coeff_b / bt709_coeff_g)
#define bt709_coeff_rg (bt709_coeff_r / bt709_coeff_g)

#define y_offset (16 << 8)
#define y_range 219
#define cr_offset (128 << 8)
#define cr_range 224
#define cr_middle (224 / 2)

#define bt709_ri_range (bt709_coeff_ri / cr_middle)
#define bt709_bi_range (bt709_coeff_bi / cr_middle)

void
beputu64(uint64_t *dest, uint64_t v)
{
    uint8_t *buf = (uint8_t *)dest;
    for (int i = 0; i < 8; i++) buf[i] = (v >> ((7 - i) * 8)) & 0xff;
}

unsigned short
clamp(unsigned short v, unsigned short min, unsigned short max)
{
    const short t = v < min ? min : v;
    return t > max ? max : t;
}

static PyObject *
method_atem_to_rgb(PyObject *self, PyObject *args)
{
    Py_buffer input_buffer;
    Py_ssize_t data_length;
    unsigned int width, height;
    PyObject *res;

    /* Parse arguments */
    if (!PyArg_ParseTuple(args, "y*II", &input_buffer, &width, &height)) {
        return NULL;
    }

    data_length = input_buffer.len;
    /* The loop reads fixed 8-byte groups (2 pixels each); a ragged length
       would read up to 7 bytes past the end of the input on the last
       group. The wire format never produces one — reject it. */
    if (data_length % 8 != 0) {
        PyBuffer_Release(&input_buffer);
        return PyErr_Format(PyExc_ValueError,
                            "frame length %zd is not a multiple of 8",
                            data_length);
    }
    if (data_length == 0) {
        /* malloc(0) may legally return NULL; don't misreport it as OOM. */
        PyBuffer_Release(&input_buffer);
        return Py_BuildValue("y#", "", (Py_ssize_t) 0);
    }
    char *buffer;
    char *resbuffer = (char *) malloc(data_length);

    if (resbuffer == NULL) {
        PyBuffer_Release(&input_buffer);
        return PyErr_NoMemory();
    }

    char *outbuffer = resbuffer;

    int pixel_size = 8;
    buffer = input_buffer.buf;
    for (Py_ssize_t i = 0; i < data_length; i += pixel_size) {
        // Convert 10-bit BT.709 Y'CbCrA 4:2:2 to RGB
        // Unpack bytes to 2xY 2xA and a B and R pair
        unsigned short a1 = (buffer[0] << 4) + ((buffer[1] & 0xf0) >> 4);
        unsigned short a2 = (buffer[4] << 4) + ((buffer[5] & 0xf0) >> 4);
        unsigned short cb = ((buffer[1] & 0x0f) << 6) + ((buffer[2] & 0xfc) >> 2);
        unsigned short cr = ((buffer[5] & 0x0f) << 6) + ((buffer[6] & 0xfc) >> 2);
        unsigned short y1 = ((buffer[2] & 0x03) << 8) + (buffer[3] & 0xff);
        unsigned short y2 = ((buffer[6] & 0x03) << 8) + (buffer[7] & 0xff);

        float cbf = bt709_bi_range * ((cb << 6) - cr_offset);
        float crf = bt709_ri_range * ((cr << 6) - cr_offset);
        float y1f = ((double) (y1 << 6) - y_offset) / y_range;
        float y2f = ((double) (y2 << 6) - y_offset) / y_range;

        // Clamp both ends: casting a negative float to unsigned char is
        // undefined behavior — on typical x86 compilers a small negative
        // wraps to a bright value, producing colored speckle on sub-black
        // or highly-saturated pixels. Match the BT.709 full-range clip.
        float r1f = fmax(0, fmin(255, y1f + crf));
        float g1f = fmax(0, fmin(255, y1f - cbf * bt709_coeff_bg - crf * bt709_coeff_rg));
        float b1f = fmax(0, fmin(255, y1f + cbf));
        float r2f = fmax(0, fmin(255, y2f + crf));
        float g2f = fmax(0, fmin(255, y2f - cbf * bt709_coeff_bg - crf * bt709_coeff_rg));
        float b2f = fmax(0, fmin(255, y2f + cbf));

        // Alpha needs the same clamp: switcher-captured stills mark opaque
        // pixels with alpha ~1023 (vs the 937 rgb_to_atem writes for 255),
        // and unclamped (1023-16)/3.6 = 279 wraps to 23 in a byte — every
        // hardware capture decoded as ~91% transparent.
        float a1f = fmax(0, fmin(255, ((double) (a1 - 16)) / 3.6));
        float a2f = fmax(0, fmin(255, ((double) (a2 - 16)) / 3.6));

        outbuffer[0] = (unsigned char) r1f;
        outbuffer[1] = (unsigned char) g1f;
        outbuffer[2] = (unsigned char) b1f;
        outbuffer[3] = (unsigned char) a1f;
        outbuffer[4] = (unsigned char) r2f;
        outbuffer[5] = (unsigned char) g2f;
        outbuffer[6] = (unsigned char) b2f;
        outbuffer[7] = (unsigned char) a2f;
        outbuffer += pixel_size;
        buffer += pixel_size;
    }

    res = Py_BuildValue("y#", resbuffer, data_length);
    free(resbuffer);
    PyBuffer_Release(&input_buffer);
    return res;
}

static PyObject *
method_rgb_to_atem(PyObject *self, PyObject *args)
{
    Py_buffer input_buffer;
    Py_ssize_t data_length;
    unsigned int width, height, premultiply;
    PyObject *res;

    /* Parse arguments */
    if (!PyArg_ParseTuple(args, "y*IIp", &input_buffer, &width, &height, &premultiply)) {
        return NULL;
    }

    data_length = input_buffer.len;
    /* Same 8-byte-group bound as atem_to_rgb: RGBA is consumed 2 pixels
       at a time, so a ragged length would over-read the final group. */
    if (data_length % 8 != 0) {
        PyBuffer_Release(&input_buffer);
        return PyErr_Format(PyExc_ValueError,
                            "frame length %zd is not a multiple of 8",
                            data_length);
    }
    if (data_length == 0) {
        PyBuffer_Release(&input_buffer);
        return Py_BuildValue("y#", "", (Py_ssize_t) 0);
    }
    unsigned char *buffer;
    buffer = input_buffer.buf;

    char *outbuffer = (char *) malloc(data_length);
    if (outbuffer == NULL) {
        PyBuffer_Release(&input_buffer);
        return PyErr_NoMemory();
    }

    char *writepointer = outbuffer;

    int pixel_size = 8;
    for (Py_ssize_t i = 0; i < data_length; i += pixel_size) {
        // Convert RGBA 8888 to 10-bit BT.709 Y'CbCrA
        float r1 = (float)buffer[0] / 255;
        float g1 = (float)buffer[1] / 255;
        float b1 = (float)buffer[2] / 255;
        float r2 = (float)buffer[4] / 255;
        float g2 = (float)buffer[5] / 255;
        float b2 = (float)buffer[6] / 255;

        if (premultiply) {
            // PNG files have straight alpha, for BMD switchers premultipled alpha is easier
            float a1 = (float)buffer[3] / 255;
            float a2 = (float)buffer[7] / 255;
            r1 = r1 * a1;
            g1 = g1 * a1;
            b1 = b1 * a1;
            r2 = r2 * a2;
            g2 = g2 * a2;
            b2 = b2 * a2;
        }

        float y1 = (0.2126f * r1) + (0.7152f * g1) + (0.0722f * b1);
        float y2 = (0.2126f * r2) + (0.7152f * g2) + (0.0722f * b2);
        float cb = (b2 - y2) / 1.8556f;
        float cr = (r2 - y2) /  1.5748f;

        unsigned short a10a = ((buffer[3] << 2) * 219 / 255) + (15 << 2) + 1;
        unsigned short a10b = ((buffer[7] << 2) * 219 / 255) + (15 << 2) + 1;
		unsigned short y10a = clamp((unsigned short) ((y1 * 876) + 64), 64, 940);
		unsigned short y10b = clamp((unsigned short) ((y2 * 876) + 64), 64, 940);
		// cb/cr can be negative (b<y or r<y); cast before clamp would
		// wrap a small negative to ~65500 and then clamp to MAX instead
		// of MIN. Pre-clamp the float to 0 to avoid the UB cast.
		unsigned short cb10 = clamp((unsigned short) fmax(0.0f, (cb * 896) + 512), 44, 960);
		unsigned short cr10 = clamp((unsigned short) fmax(0.0f, (cr * 896) + 512), 44, 960);

        writepointer[0] = (unsigned char) (a10a >> 4);
        writepointer[1] = (unsigned char) (((a10a & 0x0f) << 4) | (cb10 >> 6));
        writepointer[2] = (unsigned char) (((cb10 & 0x3f) << 2) | (y10a >> 8));
        writepointer[3] = (unsigned char) (y10a & 0xff);
        writepointer[4] = (unsigned char) (a10b >> 4);
        writepointer[5] = (unsigned char) (((a10b & 0x0f) << 4) | (cr10 >> 6));
        writepointer[6] = (unsigned char) (((cr10 & 0x3f) << 2) | (y10b >> 8));
        writepointer[7] = (unsigned char) (y10b & 0xff);
        writepointer += pixel_size;
        buffer += pixel_size;
    }

    res = Py_BuildValue("y#", outbuffer, data_length);
    free(outbuffer);
    PyBuffer_Release(&input_buffer);
    return res;
}


static PyObject *
method_rle_encode(PyObject *self, PyObject *args)
{
    Py_buffer input_buffer;
    PyObject *res;

    /* Parse arguments */
    if (!PyArg_ParseTuple(args, "y*", &input_buffer)) {
        return NULL;
    }

    /* The stream is a sequence of 8-byte words; a ragged length was
       silently truncated before (and a 1-byte input read AND wrote 8
       bytes through 1-byte buffers via the len==1 special case). */
    if (input_buffer.len % 8 != 0) {
        Py_ssize_t bad_length = input_buffer.len;
        PyBuffer_Release(&input_buffer);
        return PyErr_Format(PyExc_ValueError,
                            "RLE input length %zd is not a multiple of 8",
                            bad_length);
    }
    if (input_buffer.len == 0) {
        PyBuffer_Release(&input_buffer);
        return Py_BuildValue("y#", "", (Py_ssize_t) 0);
    }

    Py_ssize_t c = 0, i, w;
    uint64_t *data = input_buffer.buf;
    uint64_t *buf = malloc(input_buffer.len);
    if (buf == NULL) {
        PyBuffer_Release(&input_buffer);
        return PyErr_NoMemory();
    }
    for (i = 0, w = 0, c = 0; i < input_buffer.len / 8; ++i) {
        if (data[i] == RLE_HEADER) {
            /* 0xFE..FE is the run-block marker; a literal occurrence is
               unrepresentable in the stream. rgb_to_atem output can never
               contain it (byte 0 of every group is a >>4 of a 10-bit
               alpha, max 0x3A) — but native-format stills from a profile
               archive reach here unconverted, so this is operator data.
               Raise instead of emitting a stream the switcher would
               misparse. (Was assert(): compiled live, it aborted the
               whole process on such input.) */
            free(buf);
            PyBuffer_Release(&input_buffer);
            PyErr_SetString(PyExc_ValueError,
                            "RLE input contains the reserved header word "
                            "0xFEFEFEFEFEFEFEFE");
            return NULL;
        }
        if (i != 0 && data[i - 1] == data[i]) {
            ++c;
            if (i + 1 < input_buffer.len) {
                continue;
            }
        }
        if (c > 2) {
            buf[w++] = RLE_HEADER;
            beputu64(&buf[w++], c);
            buf[w++] = data[i - 1];
        } else if (c > 0) {
            for (Py_ssize_t j = 0; j < c; ++j) {
                buf[w++] = data[i - 1];
            }
        }
        buf[w++] = data[i];
        c = 0;
    }
    if (c > 2) {
        buf[w++] = RLE_HEADER;
        beputu64(&buf[w++], c);
        buf[w++] = data[i - 1];
    } else if (c > 0) {
        for (Py_ssize_t j = 0; j < c; ++j) {
            buf[w++] = data[i - 1];
        }
    }

    res = Py_BuildValue("y#", buf, w * 8);
    free(buf);
    PyBuffer_Release(&input_buffer);
    return res;
}

static PyMethodDef MediaConvertMethods[] = {
    {"atem_to_rgb", method_atem_to_rgb, METH_VARARGS, "Convert an Atem YCbCrA frame to RGB8888"},
    {"rgb_to_atem", method_rgb_to_atem, METH_VARARGS, "Convert an RGB8888 frame to Atem YCbCrA"},
    {"rle_encode",  method_rle_encode,  METH_VARARGS, "Compress data using the custom Atem RLE encoding"},
    {NULL,          NULL,               0,            NULL},
};

static struct PyModuleDef mediaconvertmodule = {
    PyModuleDef_HEAD_INIT,
    "atemwire.mediaconvert",
    "Native code for converting frames",
    -1,
    MediaConvertMethods,
};

PyMODINIT_FUNC
PyInit_mediaconvert(void)
{
    return PyModule_Create(&mediaconvertmodule);
}