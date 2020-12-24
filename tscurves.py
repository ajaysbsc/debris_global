# -*- coding: utf-8 -*-
"""
Created on Sat Aug 11 10:17:13 2018

@author: David
"""

# Built-in libraries
import argparse
import collections
import multiprocessing
import os
import pickle
import time

# External libraries
#import rasterio
#import gdal
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
import xarray as xr

# Local libraries
import debrisglobal.globaldebris_input as debris_prms
from spc_split_lists import split_list


#%% ===== FUNCTIONS =====
def getparser():
    """
    Use argparse to add arguments from the command line

    Parameters
    ----------
    batchno (optional) : int
        batch number used to differentiate output on supercomputer
    batches (optional) : int
        total number of batches based on supercomputer
    num_simultaneous_processes (optional) : int
        number of cores to use in parallels
    option_parallels (optional) : int
        switch to use parallels or not
    debug (optional) : int
        Switch for turning debug printing on or off (default = 0 (off))

    Returns
    -------
    Object containing arguments and their respective values.
    """
    parser = argparse.ArgumentParser(description="run simulations from gcm list in parallel")
    # add arguments
    parser.add_argument('-batchno', action='store', type=int, default=0,
                        help='Batch number used to differentiate output on supercomputer')
    parser.add_argument('-batches', action='store', type=int, default=1,
                        help='Total number of batches (nodes) for supercomputer')
    parser.add_argument('-latlon_fn', action='store', type=str, default=None,
                        help='Filename containing list of lat/lon tuples for running batches on spc')
    parser.add_argument('-num_simultaneous_processes', action='store', type=int, default=4,
                        help='number of simultaneous processes (cores) to use')
    parser.add_argument('-option_parallels', action='store', type=int, default=1,
                        help='Switch to use or not use parallels (1 - use parallels, 0 - do not)')
    parser.add_argument('-option_ordered', action='store', type=int, default=1,
                        help='switch to keep lists ordered or not')
    parser.add_argument('-debug', action='store', type=int, default=0,
                        help='Boolean for debugging to turn it on or off (default 0 is off')
    return parser


def create_xrdataset_ts(ds, time_values):
    """
    Create empty xarray dataset that will be used to record surface temperature from simulation runs.

    Parameters
    ----------
    ds : xarray dataset
        dataframe containing energy balance model runs

    Returns
    -------
    output_ds_all : xarray Dataset
        empty xarray dataset that contains variables and attributes to be filled in by simulation runs
    encoding : dictionary
        encoding used with exporting xarray dataset to netcdf
    """
    # Create empty datasets for each variable and merge them
    # Variable coordinates dictionary
    output_coords_dict = collections.OrderedDict()
    output_coords_dict['ts'] = collections.OrderedDict([('hd_cm', ds.hd_cm.values), ('time', time_values), 
                                                        ('elev', ds.elev.values)])
    output_coords_dict['dsnow'] = collections.OrderedDict([('hd_cm', ds.hd_cm.values), ('time', time_values), 
                                                           ('elev', ds.elev.values)])
            
    # Attributes dictionary
    output_attrs_dict = {
            'latitude': {'long_name': 'latitude',
                         'units': 'degrees north'},
            'longitude': {'long_name': 'longitude',
                          'units': 'degrees_east'},
            'roi': {'long_name': 'region of interest'},
            'time': {'long_name': 'time of satellite acquisition',
                     'temporal_resolution': 'daily'},
            'hd_cm': {'long_name': 'debris thickness',
                      'units:': 'cm'},
            'elev': {'long_name': 'elevation',
                     'units': 'm a.s.l.'},
            'ts': {'long_name': 'surface temperature',
                    'units': 'K'},
            'snow_depth': {'long_name': 'snow depth',
                           'units': 'm'}
            }
            
    assert 'ts_std' not in list(ds.keys()), 'Need to process standard deviation and add to output'
    
    # Add variables to empty dataset and merge together
    count_vn = 0
    encoding = {}
    for vn in output_coords_dict.keys():
        count_vn += 1
        empty_holder = np.zeros([len(output_coords_dict[vn][i]) for i in list(output_coords_dict[vn].keys())])
        output_ds = xr.Dataset({vn: (list(output_coords_dict[vn].keys()), empty_holder)},
                               coords=output_coords_dict[vn])
        # Merge datasets of stats into one output
        if count_vn == 1:
            output_ds_all = output_ds
        else:
            output_ds_all = xr.merge((output_ds_all, output_ds))
            
    # Add attributes
    for vn in output_ds_all.variables:
        try:
            output_ds_all[vn].attrs = output_attrs_dict[vn]
        except:
            pass
        # Encoding (specify _FillValue, offsets, etc.)
        encoding[vn] = {'_FillValue': False,
                        'zlib':True,
                        'complevel':9
                        }            
    # Add values    
    output_ds_all['latitude'] = ds['latitude']
    output_ds_all['longitude'] = ds['longitude']
    output_ds_all['hd_cm'] = ds['hd_cm']
    output_ds_all['elev']= ds['elev']
    
    # Add attributes
    output_ds_all.attrs = ds.attrs

    return output_ds_all, encoding


def main(list_packed_vars):
    """
    Model simulation

    Parameters
    ----------
    list_packed_vars : list
        list of packed variables that enable the use of parallels

    Returns
    -------
    netcdf files of the simulation output (specific output is dependent on the output option)
    """
    # Unpack variables
    count = list_packed_vars[0]
    latlon_list = list_packed_vars[1]
    
    if debug:
        print(count, latlon_list)
        
    #%%

    # Surface temperature information (year, day of year, hour)
    ts_info_fullfn = debris_prms.ts_fp + debris_prms.roi + '_debris_tsinfo.nc'
    ds_ts_info = xr.open_dataset(ts_info_fullfn, decode_times=False)
    
    for nlatlon, latlon in enumerate(latlon_list):
#    for nlatlon, latlon in enumerate([latlon_list[0]]):
#        if debug:
        print(nlatlon, latlon)
        
        lat_deg = latlon[0]
        lon_deg = latlon[1]
        #%%
        # ===== Debris Thickness vs. Surface Lowering =====        
        # Filenames            
        if lat_deg < 0:
            lat_str = 'S-'
        else:
            lat_str = 'N-'
        latlon_str = str(int(abs(lat_deg*100))) + lat_str + str(int(lon_deg*100)) + 'E-'
        if debris_prms.experiment_no == 3:
            mc_str = ''
        else:
            mc_str = str(int(debris_prms.mc_simulations)) + 'MC_'
            
        # Raw meltmodel output filename
#        ds_meltmodel_fn = debris_prms.fn_prefix + latlon_str + debris_prms.date_start + '.nc'
        ds_meltmodel_fn = debris_prms.fn_prefix + latlon_str + mc_str + debris_prms.date_start + '.nc'
        # Output processed surface temperature curve dataset
        ds_tscurve_fn = debris_prms.output_ts_fn_sample.replace('XXXX', latlon_str)
        
        if ((os.path.exists(debris_prms.tscurve_fp + ds_tscurve_fn) == False) and 
            (os.path.exists(debris_prms.eb_fp + ds_meltmodel_fn) == True)):
            
            # Dataset from energy balance modeling
            ds = xr.open_dataset(debris_prms.eb_fp + ds_meltmodel_fn)
            
            # Time information of surface temperature
            lat_idx = np.abs(lat_deg - ds_ts_info['latitude'][:].values).argmin(axis=0)
            lon_idx = np.abs(lon_deg - ds_ts_info['longitude'][:].values).argmin(axis=0)
            
            ts_year = np.round(ds_ts_info['year_mean'][lat_idx,lon_idx].values,0)
            ts_doy = np.round(ds_ts_info['doy_med'][lat_idx,lon_idx].values,0)
            ts_hr = ds_ts_info['dayfrac_mean'][lat_idx,lon_idx].values
            
#            if debug:            
#                print('ts_hr:', ts_hr, 'ts_doy:', ts_doy, 'ts_year', ts_year)
            
            if ts_year > 0 and ts_doy > 0:
                ts_str = str(int(ts_year)) + '-' + str(int(ts_doy))
                ts_date_pd = pd.to_datetime(pd.DataFrame(np.array([ts_str]))[0], format='%Y-%j')
                ts_date = ts_date_pd.values[0] + np.timedelta64(int(ts_hr),'h')
                
                # Index with model results      
                time_pd = pd.to_datetime(ds.time.values)  
                time_idx = np.where(ts_date == time_pd)[0][0]
                # index one month before and after to get statistics
                time_idx_all = np.arange(time_idx - 30*24, time_idx + 31*24, 24)
                time_all_interpolated = time_pd[time_idx_all] + np.timedelta64(int((ts_hr%1)*60),'m')
                
                # Output dataset
                ds_ts, encoding = create_xrdataset_ts(ds, time_all_interpolated)
                
                for nelev, elev_cn in enumerate(debris_prms.elev_cns):
#                    if debug:
#                        print(nelev, elev_cn)
                        
                    # Select data
                    #  each row is a debris thickness
                    ts_t1 = ds['ts'][:,time_idx_all,nelev].values
                    ts_t2 = ds['ts'][:,time_idx_all+1,nelev].values
                    ts_data = ts_t1 + ts_hr%1 * (ts_t2 - ts_t1)
                    
                    dsnow_t1 = ds['snow_depth'][:,time_idx_all,nelev].values
                    dsnow_t2 = ds['snow_depth'][:,time_idx_all+1,nelev].values
                    dsnow_data = dsnow_t1 + ts_hr%1 * (dsnow_t2 - dsnow_t1)
                    
                    ds_ts['ts'][:,:,nelev] = ts_data
                    ds_ts['dsnow'][:,:,nelev] = dsnow_data
                    
                # Export netcdf
                if os.path.exists(debris_prms.tscurve_fp) == False:
                    os.makedirs(debris_prms.tscurve_fp)
                ds_ts.to_netcdf(debris_prms.tscurve_fp + ds_tscurve_fn)
                
                #%%

    if debug:
        return ds_ts
            

#%%
    
if __name__ == '__main__':
    time_start = time.time()
    parser = getparser()
    args = parser.parse_args()

    if args.debug == 1:
        debug = True
    else:
        debug = False

    time_start = time.time()
    
    # RGI glacier number
    if args.latlon_fn is not None:
        with open(args.latlon_fn, 'rb') as f:
            latlon_list = pickle.load(f)
    else:
        latlon_list = debris_prms.latlon_list
    
    # Number of cores for parallel processing
    if args.option_parallels != 0:
        num_cores = int(np.min([len(latlon_list), args.num_simultaneous_processes]))
    else:
        num_cores = 1

    # Glacier number lists to pass for parallel processing
    latlon_lsts = split_list(latlon_list, n=num_cores, option_ordered=args.option_ordered)

    # Pack variables for multiprocessing
    list_packed_vars = []
    for count, latlon_lst in enumerate(latlon_lsts):
        list_packed_vars.append([count, latlon_lst])

    # Parallel processing
    if args.option_parallels != 0:
        print('Processing in parallel with ' + str(args.num_simultaneous_processes) + ' cores...')
        with multiprocessing.Pool(args.num_simultaneous_processes) as p:
            p.map(main,list_packed_vars)
    # If not in parallel, then only should be one loop
    else:
        # Loop through the chunks and export bias adjustments
        for n in range(len(list_packed_vars)):
            if debug and num_cores == 1:
                ds_ts = main(list_packed_vars[n])
            else:
                main(list_packed_vars[n])
     
    print('\nProcessing time of :',time.time()-time_start, 's')
    
    
    #%%
    
#debris_thicknesses = ds_ts.hd_cm.values
#fit_idx = list(np.where(debris_thicknesses >= 5)[0])
#
## Calculate statistics associated with surface temperature for each debris thickness
#ts_cns = ['debris_thickness', 'ts_degC_mean', 'ts_degC_std', 'ts_degC_med', 'ts_degC_mad']
#debris_ts_df_stats = pd.DataFrame(np.zeros((len(ds_ts['hd_cm'].values[fit_idx]),len(ts_cns))), columns=ts_cns)
#debris_ts_df_stats['debris_thickness'] = debris_thicknesses[fit_idx] / 100
#
#ts_data = ds_ts['ts'][fit_idx,:,0,0].values
#dsnow_data = ds_ts['dsnow'][fit_idx,:,0,0].values
#ts_data[dsnow_data > 0] = np.nan
#
#debris_ts_df_stats['ts_degC_mean'] = np.nanmean(ts_data, axis=1) - 273.15
#debris_ts_df_stats['ts_degC_std'] = np.nanstd(ts_data, axis=1)
#debris_ts_df_stats['ts_degC_med'] = np.nanmedian(ts_data, axis=1) - 273.15