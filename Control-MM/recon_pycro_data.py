#!/usr/bin/env python

'''
Stage scanning OPM post-processing using numpy, numba, skimage, and npy2bdv.
New version to handle altered acquisition code for multi-color large area stage scans. 
Orthgonal interpolation method as described by Vincent Maioli (http://doi.org/10.25560/68022)

Shepherd 03/20
'''

# imports
import numpy as np
from pathlib import Path
from pycromanager import Dataset
import npy2bdv
import gc
import sys
import getopt
import re
from skimage.measure import block_reduce
from skimage.util import apply_parallel
import time
from numba import njit, prange

# perform stage scanning reconstruction using orthogonal interpolation
# http://numba.pydata.org/numba-doc/latest/user/parallel.html#numba-parallel
@njit(parallel=True)
def stage_deskew(data,parameters):

    # unwrap parameters 
    theta = parameters[0]             # (degrees)
    distance = parameters[1]          # (nm)
    pixel_size = parameters[2]        # (nm)
    [num_images,ny,nx]=data.shape  # (pixels)

    # change step size from physical space (nm) to camera space (pixels)
    pixel_step = distance/pixel_size    # (pixels)

    # calculate the number of pixels scanned during stage scan 
    scan_end = num_images * pixel_step  # (pixels)

    # calculate properties for final image
    final_ny = np.int64(np.ceil(scan_end+ny*np.cos(theta*np.pi/180))) # (pixels)
    final_nz = np.int64(np.ceil(ny*np.sin(theta*np.pi/180)))          # (pixels)
    final_nx = np.int64(nx)                                           # (pixels)

    # create final image
    output = np.zeros((final_nz, final_ny, final_nx),dtype=np.float32)  # (pixels,pixels,pixels - data is float32)

    # precalculate trig functions for scan angle
    tantheta = np.float32(np.tan(theta * np.pi/180)) # (float32)
    sintheta = np.float32(np.sin(theta * np.pi/180)) # (float32)
    costheta = np.float32(np.cos(theta * np.pi/180)) # (float32)

    # perform orthogonal interpolation

    # loop through output z planes
    # defined as parallel loop in numba
    # http://numba.pydata.org/numba-doc/latest/user/parallel.html#numba-parallel
    for z in prange(0,final_nz):
        # calculate range of output y pixels to populate
        y_range_min=np.minimum(0,np.int64(np.floor(np.float32(z)/tantheta)))
        y_range_max=np.maximum(final_ny,np.int64(np.ceil(scan_end+np.float32(z)/tantheta+1)))

        # loop through final y pixels
        # defined as parallel loop in numba
        # http://numba.pydata.org/numba-doc/latest/user/parallel.html#numba-parallel
        for y in prange(y_range_min,y_range_max):

            # find the virtual tilted plane that intersects the interpolated plane 
            virtual_plane = y - z/tantheta

            # find raw data planes that surround the virtual plane
            plane_before = np.int64(np.floor(virtual_plane/pixel_step))
            plane_after = np.int64(plane_before+1)

            # continue if raw data planes are within the data range
            if ((plane_before>=0) and (plane_after<num_images)):
                
                # find distance of a point on the  interpolated plane to plane_before and plane_after
                l_before = virtual_plane - plane_before * pixel_step
                l_after = pixel_step - l_before
                
                # determine location of a point along the interpolated plane
                za = z/sintheta
                virtual_pos_before = za + l_before*costheta
                virtual_pos_after = za - l_after*costheta

                # determine nearest data points to interpoloated point in raw data
                pos_before = np.int64(np.floor(virtual_pos_before))
                pos_after = np.int64(np.floor(virtual_pos_after))

                # continue if within data bounds
                if ((pos_before>=0) and (pos_after >= 0) and (pos_before<ny-1) and (pos_after<ny-1)):
                    
                    # determine points surrounding interpolated point on the virtual plane 
                    dz_before = virtual_pos_before - pos_before
                    dz_after = virtual_pos_after - pos_after

                    # compute final image plane using orthogonal interpolation
                    output[z,y,:] = (l_before * dz_after * data[plane_after,pos_after+1,:] + \
                                    l_before * (1-dz_after) * data[plane_after,pos_after,:] + \
                                    l_after * dz_before * data[plane_before,pos_before+1,:] + \
                                    l_after * (1-dz_before) * data[plane_before,pos_before,:]) /pixel_step


    # return output
    return output

# parse experimental directory, load data, perform orthogonal deskew, and save as BDV H5 file
def main(argv):

    # parse directory name from command line argument 
    input_dir_string = ''
    output_dir_string = ''

    try:
        arguments, values = getopt.getopt(argv,"hi:o:n:c:",["help","ipath=","opath="])
    except getopt.GetoptError:
        print('Error. stage_recon.py -i <inputdirectory> -o <outputdirectory>')
        sys.exit(2)
    for current_argument, current_value in arguments:
        if current_argument == '-h':
            print('Usage. stage_recon.py -i <inputdirectory> -o <outputdirectory>')
            sys.exit()
        elif current_argument in ("-i", "--ipath"):
            input_dir_string = current_value
        elif current_argument in ("-o", "--opath"):
            output_dir_string = current_value
        
    if (input_dir_string == ''):
        print('Input parse error.')
        sys.exit(2)

    # Load data
    # this approach assumes data is generated by QI2lab pycromanager control code

    # https://docs.python.org/3/library/pathlib.html
    # Create Path object to directory
    input_dir_path=Path(input_dir_string)

    # determine number of directories in root directory. loop over each one.
    tile_dir_path = [f for f in input_dir_path.iterdir() if f.is_dir()]
    num_tiles = len(tile_dir_path)

    # TO DO: read text file with this information in root directory
    num_y = 73
    num_z = 3
    num_channels = 2
    split=40

    # create parameter array
    # [theta, stage move distance, camera pixel size]
    # units are [degrees,nm,nm]
    params=np.array([30,200,115],dtype=np.float32)

    # check if user provided output path
    if (output_dir_string==''):
        output_dir_path = input_dir_path
    else:
        output_dir_path = Path(output_dir_string)

    # https://github.com/nvladimus/npy2bdv
    # create BDV H5 file with sub-sampling for BigStitcher
    output_path = output_dir_path / 'full.h5'
    bdv_writer = npy2bdv.BdvWriter(str(output_path), nchannels=num_channels, ntiles=(num_z*num_y*split)+1, \
        subsamp=((1,1,1),(2,2,2),(4,4,4),(8,8,8),(16,16,16)),blockdim=((16, 16, 8),))

    # loop over each directory. Each directory will be placed as a "tile" into the BigStitcher file
    # TO DO: implement directory polling to do this in the background while data is being acquired.
    for tile in range(0,num_tiles):

        # load tile
        tile_dir_path_to_load = tile_dir_path[tile]
        print('Loading directory: '+str(tile_dir_path_to_load))

        # decode directory name to determine tile_id in h5. reverse Z order, normal y order
        test_string = tile_dir_path_to_load.parts[-1].split('_')
        for i in range(len(test_string)):
            if 'y0' in test_string[i]:
                y_idx = int(test_string[i].split('y')[1])
            if 'z0' in test_string[i]:
                z_idx = int(test_string[i].split('z')[1])

        tile_id = ((num_z-z_idx-1)*(num_y))+y_idx
        print('y index: '+str(y_idx)+' z index: '+str(z_idx)+' H5 tile id: '+str(tile_id))

        # https://pycro-manager.readthedocs.io/en/latest/read_data.html
        dataset = Dataset(tile_dir_path_to_load)
        
        # extract number of images in the tile
        num_x = len(dataset.axes['x'])
        num_x = num_x-10

        # loop over channels inside tile
        for channel_id in range(num_channels):

            # read images from dataset. Skip first 10 images for stage speed up
            sub_stack = np.zeros([num_x,256,1600])
            for i in range(num_x):
                sub_stack[i,:,:] = dataset.read_image(channel=channel_id, x=i+10, y=0, z=0, read_metadata=False)

            #TO DO: Integrate Microvolution hook here to do deconvolution on skewed data before deskewing.
            
            print('Deskew tile.')
            # run deskew
            deskewed = stage_deskew(data=sub_stack,parameters=params)
            del sub_stack
            gc.collect()

            # downsample by 2x in z due to oversampling when going from OPM to coverslip geometry
            deskewed_downsample = block_reduce(deskewed, block_size=(2,1,1), func=np.mean)
            del deskewed
            gc.collect()

            print('Split and write tiles.')
            # write BDV tile
            # https://github.com/nvladimus/npy2bdv 
            for split_id in range(split):
                size = np.floor(num_x/split)
                size_overlap = size * 1.1

                if split_id == split-1:
                    deskewed_downsample_substack = deskewed_downsample[split_id*size:]
                else:
                    deskewed_downsample_substack = deskewed_downsample[split_id*size:(split_id+1)*size_overlap]

                bdv_writer.append_view(deskewed_downsample_substack, time=0, channel=channel_id, 
                                        tile=(tile_id*split)+split_id,
                                        voxel_size_xyz=(.115,.115,.200), voxel_units='um')

            # free up memory
            del deskewed_downsample
            #del deskewed_downsample
            gc.collect()

    # write BDV xml file
    # https://github.com/nvladimus/npy2bdv
    bdv_writer.write_xml_file(ntimes=1)
    bdv_writer.close()

    # clean up memory
    gc.collect()


# run
if __name__ == "__main__":
    main(sys.argv[1:])


# The MIT License
#
# Copyright (c) 2020 Douglas Shepherd, Arizona State University
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.