import collections.abc
import numpy as np
import cv2
import torch
import os, time, folder_paths, math
from pathlib import Path
from PIL import Image, ImageStat, ImageFont, ImageOps, ImageDraw
from collections import abc
from itertools import repeat, product
from typing import Tuple
import scipy


# From PyTorch internals
def _ntuple(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable):
            return x
        return tuple(repeat(x, n))

    return parse


to_1tuple = _ntuple(1)
to_2tuple = _ntuple(2)
to_3tuple = _ntuple(3)
to_4tuple = _ntuple(4)
to_ntuple = _ntuple


def scanFilesInDir(input_dir):
    return sorted([f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))])


def getFont(size: int = 26, fontName: str = "Roboto-Regular.ttf"):
    nodes_path = folder_paths.get_folder_paths("custom_nodes")
    font_path = os.path.normpath(os.path.join(nodes_path[0], "ComfyUI-PixelArt-Detector/fonts/", fontName))
    if not os.path.exists(font_path):
        print(f"ERROR: {fontName} NOT FOUND!")
        return ImageFont.load_default()
    return ImageFont.truetype(str(font_path), size=size)


def calcFontSizeToFitWidthOfImage(image: Image, text: str, fontSize: int = 26, fontName: str = "Roboto-Regular.ttf"):
    # Create a draw object
    draw = ImageDraw.Draw(image)
    if not hasattr(draw, "textbbox"):
        print("ImageDraw.textbox not found. Skipping fontSize calculations!")
        return fontSize

    font = getFont(fontSize, fontName)
    text_width = draw.textbbox((0, 0), text, font)[2]

    while text_width > image.width:
        # Reduce the font size by 1
        fontSize -= 1
        print(f"Reduced font size for text '{text}' to: {fontSize} to fit the img width! Text width: {text_width} vs Image width: {image.width}")
        font = getFont(fontSize, fontName)
        # Get the new text width and height
        text_width = draw.textbbox((0, 0), text, font)[2]
        print(f"New text width: {text_width}")

    return fontSize


def transformPalette(palette: list, output: str = "image"):
    match output:
        case "image":
            palIm = Image.new('P', (1, 1))
            palIm.putpalette(palette)
            return palIm
        case "tuple":
            return paletteToTuples(palette, 3)
        case _:  # default case
            return palette


def drawTextInImage(image: Image, text, fontSize: int = 26, fontColor=(255, 0, 0), strokeColor="white"):
    # Create a draw object
    draw = ImageDraw.Draw(image)
    # Recalculate font size to fit img width
    fontSize = calcFontSizeToFitWidthOfImage(image, text, fontSize)
    font = getFont(fontSize)
    # Get the width and height of the image
    width, height = image.size
    # Get the width and height of the text
    if hasattr(draw, "textsize"):
        _, text_height = draw.textsize(text, font)
    elif hasattr(draw, "textbbox"):
        text_height = draw.textbbox((0, 0), text, font)[3]
    else:
        text_height = (font.size * len(text.split("\n"))) + 6

    # Calculate the position of the text
    x = 0  # left margin
    y = height - (text_height + 5)  # bottom margin
    # Draw the text on the image
    draw.text((x, y), text, font=font, fill=fontColor, stroke_width=2, stroke_fill=strokeColor)


def getPalettesPath():
    nodes_path = folder_paths.get_folder_paths("custom_nodes")
    full_pallete_path = os.path.normpath(os.path.join(nodes_path[0], "ComfyUI-PixelArt-Detector/palettes/"))
    return Path(full_pallete_path)


def getPaletteImage(palette_from_image):
    full_pallete_path = os.path.normpath(os.path.join(getPalettesPath(), palette_from_image))
    return Path(full_pallete_path)


def paletteToTuples(palette, n):
    return list(zip(*[iter(palette)] * n))  # zip the array with itself n times and convert it to list


# Tensor to PIL
def tensor2pil(image):
    return Image.fromarray(np.clip(255. * image.cpu().numpy().squeeze(), 0, 255).astype(np.uint8))


def pil2tensor(image):
    return torch.from_numpy(np.array(image).astype(np.float32) / 255.0).unsqueeze(0)


def reducePalette(image, reduce_palette_max_colors):
    # Reduce color palette using elbow method
    best_k = determine_best_k(image, reduce_palette_max_colors)
    return image.quantize(colors=best_k, method=1, kmeans=best_k, dither=0).convert('RGB'), best_k


def getQuantizeMethod(method: str) -> int:
    # a dictionary that maps each string option to a quantize value
    switch = {
        "MEDIANCUT": Image.Quantize.MEDIANCUT,
        "MAXCOVERAGE": Image.Quantize.MAXCOVERAGE,
        "FASTOCTREE": Image.Quantize.FASTOCTREE
    }

    # Return the corresponding quantize value from the dictionary, or MEDIANCUT if not found
    return switch.get(method, None)


def ditherBayer(im, pal_im, order):
    def _normalized_bayer_matrix(n):
        if n == 0:
            return np.zeros((1, 1), "float32")
        else:
            q = 4 ** n
            m = q * _normalized_bayer_matrix(n - 1)
            return np.bmat(((m - 1.5, m + 0.5), (m + 1.5, m - 0.5))) / q

    num_colors = len(pal_im.getpalette()) // 3
    spread = 2 * 256 / num_colors
    bayer_n = int(math.log2(order))
    bayer_matrix = torch.from_numpy(spread * _normalized_bayer_matrix(bayer_n) + 0.5)

    result = torch.from_numpy(np.array(im).astype(np.float32))
    tw = math.ceil(result.shape[0] / bayer_matrix.shape[0])
    th = math.ceil(result.shape[1] / bayer_matrix.shape[1])
    tiled_matrix = bayer_matrix.tile(tw, th).unsqueeze(-1)
    result.add_(tiled_matrix[:result.shape[0], :result.shape[1]]).clamp_(0, 255)
    result = result.to(dtype=torch.uint8)

    return Image.fromarray(result.cpu().numpy())


def npQuantize(image: Image, palette: list) -> Image:
    colors = np.asarray(palette)
    pix = np.asarray(image.convert(None))
    # use NumPy’s broadcasting feature to subtract each of the palettes color from each pixel in the image
    # each element in the resulting array represents the difference between a pixel and a color in terms of RGB values
    subs = pix - colors[:, None, None]
    # use NumPy’s einsum function to calculate the squared Euclidean distance between each pixel and each color
    # then use NumPy’s argmin function to find the index of the minimum value along the first axis (the color axis)
    # finally, this line uses NumPy’s indexing feature to select the corresponding palette color for each pixel from the colors array
    # the out array represents an image that has been converted to use only the palette's colors
    out = colors[np.einsum('ijkl,ijkl->ijk', subs, subs).argmin(0)]
    return Image.fromarray(out.astype('uint8'), 'RGB')


# https://theartofdoingcs.com/blog/f/bit-me
def pixelate(image: Image, grid_size: int, palette: list):
    if len(palette) > 0:
        if not isinstance(palette[0], tuple):
            palette = paletteToTuples(palette, 3)

    pixel_image = Image.new('RGB', image.size)

    for i in range(0, image.size[0], grid_size):
        for j in range(0, image.size[1], grid_size):
            pixel_box = (i, j, i + grid_size, j + grid_size)
            current = image.crop(pixel_box)

            median_color = ImageStat.Stat(current).median
            median_color = tuple(median_color)

            closest_color = distance(median_color, palette)
            median_pixel = Image.new('RGB', (grid_size, grid_size), closest_color)
            pixel_image.paste(median_pixel, (i, j))

    return pixel_image


def distance(median_color, palette: list[tuple]):
    (r1, g1, b1) = median_color

    colors = {}

    for color in palette:
        (r2, g2, b2) = color
        distance = ((r2 - r1) ** 2 + (g2 - g1) ** 2 + (b2 - b1) ** 2)
        colors[distance] = color

    closest_distance = min(colors.keys())
    closest_color = colors[closest_distance]

    return closest_color


def determine_best_k(image: Image, max_k: int):
    # Convert the image to RGB mode
    image = image.convert("RGB")

    # Prepare arrays for distortion calculation
    pixels = np.array(image)
    pixel_indices = np.reshape(pixels, (-1, 3))

    # Calculate distortion for different values of k
    distortions = []
    for k in range(1, max_k + 1):
        quantized_image = image.quantize(colors=k, method=0, kmeans=k, dither=0)
        centroids = np.array(quantized_image.getpalette()[:k * 3]).reshape(-1, 3)

        # Calculate distortions
        distances = np.linalg.norm(pixel_indices[:, np.newaxis] - centroids, axis=2)
        min_distances = np.min(distances, axis=1)
        distortions.append(np.sum(min_distances ** 2))

    # Calculate the rate of change of distortions
    rate_of_change = np.diff(distortions) / np.array(distortions[:-1])

    # Find the elbow point (best k value)
    if len(rate_of_change) == 0:
        best_k = 2
    else:
        elbow_index = np.argmax(rate_of_change) + 1
        best_k = elbow_index + 2

    return best_k


def kCentroid(image: Image, width: int, height: int, centroids: int):
    image = image.convert("RGB")

    # Create an empty array for the downscaled image
    downscaled = np.zeros((height, width, 3), dtype=np.uint8)

    print(f"Size detected and reduced to \033[93m{width}\033[0m x \033[93m{height}\033[0m")

    # Calculate the scaling factors
    wFactor = image.width / width
    hFactor = image.height / height

    # Iterate over each tile in the downscaled image
    for x, y in product(range(width), range(height)):
        # Crop the tile from the original image
        tile = image.crop((x * wFactor, y * hFactor, (x * wFactor) + wFactor, (y * hFactor) + hFactor))

        # Quantize the colors of the tile using k-means clustering
        tile = tile.quantize(colors=centroids, method=1, kmeans=centroids).convert("RGB")

        # Get the color counts and find the most common color
        color_counts = tile.getcolors()
        most_common_color = max(color_counts, key=lambda x: x[0])[1]

        # Assign the most common color to the corresponding pixel in the downscaled image
        downscaled[y, x, :] = most_common_color

    return Image.fromarray(downscaled, mode='RGB')


def pixel_detect(image: Image):
    # [Astropulse]
    # Thanks to https://github.com/paultron for optimizing my garbage code 
    # I swapped the axis so they accurately reflect the horizontal and vertical scaling factor for images with uneven ratios

    # Convert the image to a NumPy array
    npim = np.array(image)[..., :3]

    # Compute horizontal differences between pixels
    hdiff = np.sqrt(np.sum((npim[:, :-1, :] - npim[:, 1:, :]) ** 2, axis=2))
    hsum = np.sum(hdiff, 0)

    # Compute vertical differences between pixels
    vdiff = np.sqrt(np.sum((npim[:-1, :, :] - npim[1:, :, :]) ** 2, axis=2))
    vsum = np.sum(vdiff, 1)

    # Find peaks in the horizontal and vertical sums
    hpeaks, _ = scipy.signal.find_peaks(hsum, distance=1, height=0.0)
    vpeaks, _ = scipy.signal.find_peaks(vsum, distance=1, height=0.0)

    # Compute spacing between the peaks
    hspacing = np.diff(hpeaks)
    vspacing = np.diff(vpeaks)

    # Resize input image using kCentroid with the calculated horizontal and vertical factors
    return kCentroid(
        image,
        round(image.width / np.median(hspacing)),
        round(image.height / np.median(vspacing)),
        2
    )


# Converts a Tensor into a Numpy array
# |imtype|: the desired type of the converted numpy array
def tensor2im(image_tensor, imtype=np.uint8, normalize=True):
    # Check if the image_tensor is a list of tensors
    if isinstance(image_tensor, list):
        # Initialize an empty list to store the converted images
        image_numpy = []
        # Loop through each tensor in the list
        for i in range(len(image_tensor)):
            # Recursively call the tensor2im function on each tensor and append the result to the list
            image_numpy.append(tensor2im(image_tensor[i], imtype, normalize))
        # Return the list of converted images
        return image_numpy
    # If the image_tensor is not a list, convert it to a NumPy array on the CPU with float data type
    image_numpy = image_tensor.cpu().float().numpy()

    # Check if the normalize parameter is True
    if normalize:
        # This will scale the pixel values from [-1, 1] to [0, 255]
        image_numpy = (np.transpose(image_numpy, (1, 2, 0)) + 1) / 2.0 * 255.0
    else:
        # This will scale the pixel values from [0, 1] to [0, 255]
        image_numpy = np.transpose(image_numpy, (1, 2, 0)) * 255.0

        # Clip the pixel values to the range [0, 255] to avoid overflow or underflow
    image_numpy = np.clip(image_numpy, 0, 255)
    # Check if the array has one or more than three channels
    if image_numpy.shape[2] == 1 or image_numpy.shape[2] > 3:
        # If so, select only the first channel and discard the rest
        # This will convert the array to grayscale
        image_numpy = image_numpy[:, :, 0]
    # Return the array with the specified data type (default is unsigned 8-bit integer)
    return image_numpy.astype(imtype)


# flags:
# - cv2.KMEANS_RANDOM_CENTERS: it always starts with a random set of initial samples, and tries to converge from there depending upon TermCriteria. Fast but doesn't guarantee same labels for the exact same image. Needs more "attempts" to find the "best" labels
# - cv2.KMEANS_PP_CENTERS: it first iterates the whole image to determine the probable centers and then starts to converge. Slow but will yield optimum and consistent results for same input image.
def get_cv2_kmeans_flags(method: str) -> int:
    switch = {
        "RANDOM_CENTERS": cv2.KMEANS_RANDOM_CENTERS,
        "PP_CENTERS": cv2.KMEANS_PP_CENTERS,
    }

    return switch.get(method, cv2.KMEANS_PP_CENTERS)


# input must be BGR cv2 image
def cv2_quantize(image, max_k: int, flags=cv2.KMEANS_RANDOM_CENTERS, attempts: int = 10,
                 criteriaMaxIterations: int = 10, criteriaMinAccuracy: float = 1.0) -> np.ndarray:
    """Performs color quantization using K-means clustering algorithm"""
    # Reshape the image into a 2D array of pixels and convert it to float32 type
    # pixels = np.array(original_image).reshape((-1, 3)).astype(np.float32)

    image = np.array(image, dtype=np.float32)

    # Reshape image to (n, 3)
    rows, cols, channels = image.shape
    assert channels == 3
    image = image.reshape((rows * cols, channels))

    # Define criteria
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, criteriaMaxIterations, criteriaMinAccuracy)

    # Apply k-means clustering
    print(
        f"Running opencv.kmeans. Flags: {flags}, attempts: {attempts}, criteriaMaxIterations: {criteriaMaxIterations}, criteriaMinAccuracy: {criteriaMinAccuracy}, max_k: {max_k}")
    compactness, labels, centers = cv2.kmeans(image, max_k, None, criteria, attempts, flags)

    # Convert centers to uint8 and index with labels
    centers = np.uint8(centers)
    quantized = centers[labels.flatten()]

    # Convert the centers to uint8 type and reshape them into a 3D array of colors
    # colors = centers.astype(np.uint8).reshape((-1 ,3))
    # Use the labels to assign each pixel in the original image to its corresponding color from the centers array
    # quantized_pixels = colors[labels.flatten()]
    # Reshape the result into a 3D array of pixels and convert it to uint8 type
    # quantized_image = quantized_pixels.reshape(original_image.size[::-1] + (3 ,)).astype(np.uint8)

    # cv2 image
    return quantized.reshape((rows, cols, channels))


def tensor2cv2img(tensor) -> np.ndarray:
    # Move the tensor to the CPU if needed
    tensor = tensor.cpu()
    array = tensor.numpy()
    # Transpose the array to change the shape from (3, 100, 100) to (100, 100, 3)
    array = array.transpose(1, 2, 0)
    # Convert the color space from RGB to BGR
    return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)


def convert_from_cv2_to_image(img: np.ndarray) -> Image:
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def convert_from_image_to_cv2(img: Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def cv2img2tensor(imgs, bgr2rgb=True, float32=True):
    """Numpy array to tensor.

    Args:
        imgs (list[ndarray] | ndarray): Input images.
        bgr2rgb (bool): Whether to change bgr to rgb.
        float32 (bool): Whether to change to float32.

    Returns:
        list[tensor] | tensor: Tensor images. If returned results only have
            one element, just return tensor.
    """

    def _totensor(img, bgr2rgb, float32):
        if img.shape[2] == 3 and bgr2rgb:
            if img.dtype == 'float64':
                img = img.astype('float32')
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        img = torch.from_numpy(img.transpose(2, 0, 1))
        if float32:
            img = img.float()
        return img

    if isinstance(imgs, list):
        return [_totensor(img, bgr2rgb, float32) for img in imgs]
    else:
        return _totensor(imgs, bgr2rgb, float32)


# From https://github.com/Chadys/QuantizeImageMethods
def cleanupColors(image: Image, threshold_pixel_percentage: float, nb_colours: int, method):
    nb_pixels: int = image.width * image.height
    quantized_img: Image.Image
    while True:
        print(f"Attempt quantizing with colors: {nb_colours}")
        quantized_img = image.quantize(colors=nb_colours, method=method, kmeans=nb_colours)
        nb_colours_under_threshold = 0
        colours_list: [Tuple[int, int]] = quantized_img.getcolors(nb_colours)
        for (count, pixel) in colours_list:
            if count / nb_pixels < threshold_pixel_percentage:
                nb_colours_under_threshold += 1
        print(f"Colors under threshold: {nb_colours_under_threshold}")
        if nb_colours_under_threshold == 0:
            break
        nb_colours -= -(-nb_colours_under_threshold // 2)  # ceil integer division

    palette: [int] = quantized_img.getpalette()
    colours_list: [[int]] = [palette[i: i + 3] for i in range(0, nb_colours * 3, 3)]
    print(f"Colors list: {colours_list}")

    return quantized_img.convert("RGB")


# From WAS Node Suite
def smart_grid_image(images: list, cols=6, size=(256, 256), add_border=True, border_color=(255, 255, 255),
                     border_width=3):
    cols = min(cols, len(images))
    # calculate row height
    max_width, max_height = size
    row_height = 0
    images_resized = []

    if add_border == False:
        border_width = 1

    for img in images:
        img_w, img_h = img.size
        aspect_ratio = img_w / img_h
        if aspect_ratio > 1:  # landscape
            thumb_w = min(max_width, img_w - border_width)
            thumb_h = thumb_w / aspect_ratio
        else:  # portrait
            thumb_h = min(max_height, img_h - border_width)
            thumb_w = thumb_h * aspect_ratio

        # pad the image to match the maximum size and center it within the cell
        pad_w = max_width - int(thumb_w)
        pad_h = max_height - int(thumb_h)
        left = pad_w // 2
        top = pad_h // 2
        right = pad_w - left
        bottom = pad_h - top
        padding = (left, top, right, bottom)  # left, top, right, bottom
        img_resized = ImageOps.expand(img.resize((int(thumb_w), int(thumb_h))), padding)

        # if add_border:
        #     img_resized = ImageOps.expand(img_resized, border=border_width//2, fill=border_color)

        images_resized.append(img_resized)
        row_height = max(row_height, img_resized.size[1])
    row_height = int(row_height)

    # calculate the number of rows
    total_images = len(images_resized)
    rows = math.ceil(total_images / cols)

    # create empty image to put thumbnails
    new_image = Image.new('RGB',
                          (cols * size[0] + (cols - 1) * border_width, rows * row_height + (rows - 1) * border_width),
                          border_color)

    for i, img in enumerate(images_resized):
        if add_border:
            border_img = ImageOps.expand(img, border=border_width // 2, fill=border_color)
            x = (i % cols) * (size[0] + border_width)
            y = (i // cols) * (row_height + border_width)
            if border_img.size == (size[0], size[1]):
                new_image.paste(border_img, (x, y, x + size[0], y + size[1]))
            else:
                # Resize image to match size parameter
                border_img = border_img.resize((size[0], size[1]))
                new_image.paste(border_img, (x, y, x + size[0], y + size[1]))
        else:
            x = (i % cols) * (size[0] + border_width)
            y = (i // cols) * (row_height + border_width)
            if img.size == (size[0], size[1]):
                new_image.paste(img, (x, y, x + img.size[0], y + img.size[1]))
            else:
                # Resize image to match size parameter
                img = img.resize((size[0], size[1]))
                new_image.paste(img, (x, y, x + size[0], y + size[1]))

    new_image = ImageOps.expand(new_image, border=border_width, fill=border_color)

    return new_image
