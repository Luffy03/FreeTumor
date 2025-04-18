### Tumor Generateion
import random
import cv2
import elasticdeform
import numpy as np
from scipy.ndimage import gaussian_filter
import torch


def generate_prob_function(mask_shape):
    sigma = np.random.uniform(3, 15)
    # uniform noise generate
    a = np.random.uniform(0, 1, size=(mask_shape[0], mask_shape[1], mask_shape[2]))

    # Gaussian filter
    # this taks some time
    a_2 = gaussian_filter(a, sigma=sigma)

    scale = np.random.uniform(0.19, 0.21)
    base = np.random.uniform(0.04, 0.06)
    a = scale * (a_2 - np.min(a_2)) / (np.max(a_2) - np.min(a_2)) + base

    return a


# first generate 5*200*200*200
def get_texture(mask_shape):
    # get the prob function
    a = generate_prob_function(mask_shape)

    # sample once
    random_sample = np.random.uniform(0, 1, size=(mask_shape[0], mask_shape[1], mask_shape[2]))

    # if a(x) > random_sample(x), set b(x) = 1
    b = (a > random_sample).astype(float)  # int type can't do Gaussian filter

    # Gaussian filter
    if np.random.uniform() < 0.7:
        sigma_b = np.random.uniform(3, 5)
    else:
        sigma_b = np.random.uniform(5, 8)

    # this takes some time
    b2 = gaussian_filter(b, sigma_b)

    # Scaling and clipping
    u_0 = np.random.uniform(0.5, 0.55)
    threshold_mask = b2 > 0.12  # this is for calculte the mean_0.2(b2)
    beta = u_0 / (np.sum(b2 * threshold_mask) / threshold_mask.sum())
    Bj = np.clip(beta * b2, 0, 1)  # 目前是0-1区间

    return Bj


# here we want to get predefined texutre:
def get_predefined_texture(mask_shape, sigma_a, sigma_b):
    # uniform noise generate
    a = np.random.uniform(0, 1, size=(mask_shape[0], mask_shape[1], mask_shape[2]))
    a_2 = gaussian_filter(a, sigma=sigma_a)
    scale = np.random.uniform(0.19, 0.21)
    base = np.random.uniform(0.04, 0.06)
    a = scale * (a_2 - np.min(a_2)) / (np.max(a_2) - np.min(a_2)) + base

    # sample once
    random_sample = np.random.uniform(0, 1, size=(mask_shape[0], mask_shape[1], mask_shape[2]))
    b = (a > random_sample).astype(float)  # int type can't do Gaussian filter
    b = gaussian_filter(b, sigma_b)

    # Scaling and clipping
    u_0 = np.random.uniform(0.5, 0.55)
    threshold_mask = b > 0.12  # this is for calculte the mean_0.2(b2)
    beta = u_0 / (np.sum(b * threshold_mask) / threshold_mask.sum())
    Bj = np.clip(beta * b, 0, 1)  # 目前是0-1区间

    return Bj


# Step 1: Random select (numbers) location for tumor.
def random_select(mask_scan):
    organ_mask = mask_scan.copy()
    organ_mask[mask_scan != 1] = 0

    if organ_mask.sum() == 0:
        z_start, z_end = 0, organ_mask.shape[-1]
    else:
        # we first find z index and then sample point with z slice
        z_start, z_end = np.where(np.any(organ_mask, axis=(0, 1)))[0][[0, -1]]

    # we need to strict number z's position (0.3 - 0.7 in the middle of liver)
    z = round(random.uniform(0.3, 0.7) * (z_end - z_start)) + z_start

    liver_mask = organ_mask[..., z]

    # erode the mask (we don't want the edge points)
    kernel = np.ones((5, 5), dtype=np.uint8)
    liver_mask = cv2.erode(liver_mask, kernel, iterations=1)

    coordinates = np.argwhere(liver_mask == 1)
    if len(coordinates) > 0:
        random_index = np.random.randint(0, len(coordinates))
        xyz = coordinates[random_index].tolist()  # get x,y
        xyz.append(z)
        potential_points = xyz
    else:
        potential_points = [0, 0, 0]

    return potential_points


# Step 2 : generate the ellipsoid
def get_ellipsoid(x, y, z):
    """"
    x, y, z is the radius of this ellipsoid in x, y, z direction respectly.
    """
    sh = (4 * x, 4 * y, 4 * z)
    out = np.zeros(sh, int)
    aux = np.zeros(sh)
    radii = np.array([x, y, z])
    com = np.array([2 * x, 2 * y, 2 * z])  # center point

    # calculate the ellipsoid
    bboxl = np.floor(com - radii).clip(0, None).astype(int)
    bboxh = (np.ceil(com + radii) + 1).clip(None, sh).astype(int)
    roi = out[tuple(map(slice, bboxl, bboxh))]
    roiaux = aux[tuple(map(slice, bboxl, bboxh))]
    logrid = *map(np.square, np.ogrid[tuple(
        map(slice, (bboxl - com) / radii, (bboxh - com - 1) / radii, 1j * (bboxh - bboxl)))]),
    dst = (1 - sum(logrid)).clip(0, None)
    mask = dst > roiaux
    roi[mask] = 1
    np.copyto(roiaux, dst, where=mask)

    return out


def get_fixed_geo(mask_scan, tumor_type, num_tumor):
    enlarge_x, enlarge_y, enlarge_z = 160, 160, 160
    geo_mask = np.zeros(
        (mask_scan.shape[0] + enlarge_x, mask_scan.shape[1] + enlarge_y, mask_scan.shape[2] + enlarge_z), dtype=np.int8)
    # texture_map = np.zeros((mask_scan.shape[0] + enlarge_x, mask_scan.shape[1] + enlarge_y, mask_scan.shape[2] + enlarge_z), dtype=np.float16)
    tiny_radius, small_radius, medium_radius, large_radius = 4, 8, 16, 32

    if tumor_type == 'tiny':
        if num_tumor is None:
            num_tumor = random.randint(3, 10)

        for _ in range(num_tumor):
            # Tiny tumor
            x = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            y = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            z = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            sigma = random.uniform(0.5, 1)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo

    if tumor_type == 'small':
        if num_tumor is None:
            num_tumor = random.randint(3, 10)
        for _ in range(num_tumor):
            # Small tumor
            x = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            y = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            z = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            sigma = random.randint(1, 2)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

    if tumor_type == 'medium':
        if num_tumor is None:
            num_tumor = random.randint(2, 5)
        for _ in range(num_tumor):
            # medium tumor
            x = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            y = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            z = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            sigma = random.randint(3, 6)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste medium tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

    if tumor_type == 'large':
        if num_tumor is None:
            num_tumor = random.randint(1, 3)
        for _ in range(num_tumor):
            # Large tumor
            x = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            y = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            z = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            sigma = random.randint(5, 10)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

    if tumor_type == "mix":
        # tiny
        num_tumor = random.randint(3, 10)
        for _ in range(num_tumor):
            # Tiny tumor
            x = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            y = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            z = random.randint(int(0.75 * tiny_radius), int(1.25 * tiny_radius))
            sigma = random.uniform(0.5, 1)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo

        # small
        num_tumor = random.randint(5, 10)
        for _ in range(num_tumor):
            # Small tumor
            x = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            y = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            z = random.randint(int(0.75 * small_radius), int(1.25 * small_radius))
            sigma = random.randint(1, 2)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

        # medium
        num_tumor = random.randint(2, 5)
        for _ in range(num_tumor):
            # medium tumor
            x = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            y = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            z = random.randint(int(0.75 * medium_radius), int(1.25 * medium_radius))
            sigma = random.randint(3, 6)

            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste medium tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

        # large
        num_tumor = random.randint(1, 3)
        for _ in range(num_tumor):
            # Large tumor
            x = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            y = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            z = random.randint(int(0.75 * large_radius), int(1.25 * large_radius))
            sigma = random.randint(5, 10)
            geo = get_ellipsoid(x, y, z)
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 1))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(1, 2))
            geo = elasticdeform.deform_random_grid(geo, sigma=sigma, points=3, order=0, axis=(0, 2))
            # texture = get_texture((4*x, 4*y, 4*z))
            point = random_select(mask_scan)
            new_point = [point[0] + enlarge_x // 2, point[1] + enlarge_y // 2, point[2] + enlarge_z // 2]
            x_low, x_high = new_point[0] - geo.shape[0] // 2, new_point[0] + geo.shape[0] // 2
            y_low, y_high = new_point[1] - geo.shape[1] // 2, new_point[1] + geo.shape[1] // 2
            z_low, z_high = new_point[2] - geo.shape[2] // 2, new_point[2] + geo.shape[2] // 2

            # paste small tumor geo into test sample
            geo_mask[x_low:x_high, y_low:y_high, z_low:z_high] += geo
            # texture_map[x_low:x_high, y_low:y_high, z_low:z_high] = texture

    geo_mask = geo_mask[enlarge_x // 2:-enlarge_x // 2, enlarge_y // 2:-enlarge_y // 2, enlarge_z // 2:-enlarge_z // 2]
    # texture_map = texture_map[enlarge_x//2:-enlarge_x//2, enlarge_y//2:-enlarge_y//2, enlarge_z//2:-enlarge_z//2]
    geo_mask = (geo_mask * mask_scan) >= 1

    return geo_mask


def get_tumor(volume_scan, mask_scan, tumor_type, texture, num_tumor=None, difference=None):
    mask_scan_numpy = mask_scan.clone().data.cpu().numpy()
    geo_mask = get_fixed_geo(mask_scan_numpy, tumor_type, num_tumor)

    sigma = np.random.uniform(1, 2)

    if difference is None:
        difference = torch.rand(1)
        difference = 0.5 * difference + 0.2  # difference from 0.2~0.7

    difference = difference.to(volume_scan.device)

    # blur the boundary
    geo_blur = gaussian_filter(geo_mask * 1.0, sigma)
    geo_blur = torch.from_numpy(geo_blur).to(volume_scan.device)

    texture = torch.from_numpy(texture).to(volume_scan.device)

    # Now, mask_scan contain: 0 background, 1 organ, 2 real tumor. Get organ_mask
    organ_mask = mask_scan.clone()
    organ_mask[mask_scan != 1] = 0

    # we only generate fake tumor on the organ mask. Then we learn the differences for synthesis !!!
    abnormally = (volume_scan - geo_blur * difference * texture) * organ_mask

    # When it is not organ_mask, remain the original volume. Otherwise, the new fake tumor and organ (abnormal)
    abnormally_full = (1 - organ_mask) * volume_scan + abnormally

    # Generate abnormally_mask (labels), if geo_mask is true, then there are Fake Tumor (class 3) !
    abnormally_mask = mask_scan.clone()
    abnormally_mask[geo_mask] = 3
    # Important: geo mask may overlap real tumor, we need to recover it
    abnormally_mask[mask_scan == 2] = 2

    return abnormally_full, abnormally_mask


def SynthesisTumor(volume_scan, mask_scan, tumor_type, texture, num_tumor=None, difference=None):
    mask_scan_numpy = mask_scan.data.cpu().numpy()

    # For online generation !!!
    x_start, x_end = 0, mask_scan_numpy.shape[0]
    y_start, y_end = 0, mask_scan_numpy.shape[1]
    z_start, z_end = 0, mask_scan_numpy.shape[2]

    organ_volume = volume_scan[x_start:x_end, y_start:y_end, z_start:z_end]
    organ_mask = mask_scan[x_start:x_end, y_start:y_end, z_start:z_end]

    # input texture shape: 420, 300, 320
    # we need to cut it into the shape of organ_mask
    # for examples, the organ_mask.shape = 286, 173, 46; we should change the texture shape
    x_length, y_length, z_length = x_end - x_start, y_end - y_start, z_end - z_start
    start_x = random.randint(0, texture.shape[
        0] - x_length - 1)  # random select the start point, -1 is to avoid boundary check
    start_y = random.randint(0, texture.shape[1] - y_length - 1)
    start_z = random.randint(0, texture.shape[2] - z_length - 1)

    cut_texture = texture[start_x:start_x + x_length, start_y:start_y + y_length, start_z:start_z + z_length]
    cut_difference = difference[x_start:x_end, y_start:y_end, z_start:z_end] if difference is not None else None

    organ_volume, organ_mask = get_tumor(organ_volume, organ_mask, tumor_type, cut_texture, num_tumor, cut_difference)

    volume_scan[x_start:x_end, y_start:y_end, z_start:z_end] = organ_volume
    mask_scan[x_start:x_end, y_start:y_end, z_start:z_end] = organ_mask

    return volume_scan, mask_scan
