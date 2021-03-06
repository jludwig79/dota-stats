# -*- coding: utf-8 -*-
"""Utility functions used by multiple scripts in the project. Includes:

    - MatchSerialization
    - Bitmask
    - MLEncoding

See individual methods for more information.
"""

from datetime import datetime
import numpy as np
from dota_stats import meta


class TimeMethods:
    """Methods to handle time string/format mangling"""

    @classmethod
    def get_time_nearest(cls, timestamp, hour=True):
        """Return timestamp and string to nearest hour or day."""

        utc = datetime.utcfromtimestamp(timestamp)
        if hour is True:
            dt_hour = datetime(utc.year, utc.month, utc.day, utc.hour, 0)
        else:
            dt_hour = datetime(utc.year, utc.month, utc.day, 0, 0)
        dt_str = dt_hour.strftime("%Y%m%d_%H%M")
        itime = int((dt_hour - datetime(1970, 1, 1)).total_seconds())

        return itime, dt_str

    @classmethod
    def get_hour_blocks(cls, timestamp, hours):
        """Given `timestamp`, return list of begin and end times on the near
        hour
        going back `hours` from the timestamp."""

        # Timestamps relative to most recent match in database
        time_hr, _ = cls.get_time_nearest(timestamp)

        begin = []
        end = []
        text = []

        for i in range(int(hours)):
            end.append(time_hr - i * 3600)
            begin.append(time_hr - (i + 1) * 3600)
            _, time_str = cls.get_time_nearest(end[-1])
            text.append(time_str)

        return text, begin, end


class MLEncoding:
    """Methods to one-hot encode and decode hero information for machine
    learning applications.
    """

    @staticmethod
    def first_order_vector(rad_heroes, dire_heroes):
        """Generate vector encoding hero selections. Length
        of vector is 2N, where [0:N] are {0,1} indicating radiant
        selection, and [N:N2] are {0,1} indicating dire.

            1 = Radiant
            -1 = Dire

        """
        # Placeholder for results
        x1_data = np.zeros([len(rad_heroes), meta.NUM_HEROES*2], dtype=np.int8)

        # For each row, create five repeated indicies so we can unroll
        # the list of heroes
        idx_rows = []
        for counter in range(len(rad_heroes)):
            idx_rows.extend(5*[counter])
        idx_rows = np.array(idx_rows)

        # For radiant, just unroll and convert to hero index from
        # hero number.
        idx_rad = np.array(rad_heroes).reshape(-1)
        idx_rad = np.array([meta.HEROES.index(t) for t in idx_rad])
        x1_data[(idx_rows, idx_rad)] = 1

        # For dire, offset by number of heroes
        idx_dire = np.array(dire_heroes).reshape(-1)
        idx_dire = np.array([meta.HEROES.index(t)+meta.NUM_HEROES for t in
                             idx_dire])
        x1_data[(idx_rows, idx_dire)] = -1

        return x1_data

    @staticmethod
    def second_order_hmatrix(rad_heroes, dire_heroes):
        """For a list of radiant and dire heroes, create an upper triangular
        matrix indicating radiant/dire pairs. By convention, 1 indicates
        first hero in i,j is on dire, -1 indicates first hero was on dire.
        See README.md for more information.

        rad_heroes: radiant heroes, numerical by enum
        dire_heroes: radiant heroes, numerical by enum

        """
        data_x2 = np.zeros([meta.NUM_HEROES, meta.NUM_HEROES], dtype=np.int8)
        for rad_hero in rad_heroes:
            for dire_hero in dire_heroes:
                irh = meta.HEROES.index(rad_hero)
                idh = meta.HEROES.index(dire_hero)
                if idh > irh:
                    data_x2[irh, idh] = 1
                if idh < irh:
                    data_x2[idh, irh] = -1
                if idh == irh:
                    raise ValueError("Duplicate heroes: {} {}".format(
                        rad_heroes, dire_heroes))
        return data_x2

    @staticmethod
    def flatten_second_order_upper(x2_matrix):
        """Unravel upper triangular matrix into flat vector, skipping
        diagonal. See README.md for more information."""
        size = x2_matrix.shape[1]

        idx = np.triu_indices(n=size, k=1)
        x_flat = x2_matrix[idx].copy()

        return x_flat

    @staticmethod
    def unflatten_second_order_upper(x_flat, mirror=True):
        """Create upper triangular matrix from flat vector, skipping
        diagonal. Mirror controls whether or not the value is reflected
        over the diagonal.

        See README.md for more information."""
        matrix_size = int((1+(1+8*x_flat.shape[0])**0.5)/2)
        x_matrix = np.zeros([matrix_size, matrix_size])
        counter = 0
        for i in range(matrix_size):
            for j in [t+i+1 for t in range(matrix_size-i-1)]:
                x_matrix[i, j] = x_flat[counter]
                if mirror:
                    x_matrix[j, i] = -x_flat[counter]
                counter = counter+1
        return x_matrix

    @classmethod
    def create_features(cls, radiant_heroes, dire_heroes,
                        radiant_win, verbose=True):
        """Main entry point to create one-hot encodings for machine learning.

        Input:

            radiant_heroes: list of lists, heroes on radiant in each match
            dire_heroes:    list of lists, heroes on dire in each match
            radiant_win:    boolean, radiant win flag

        Returns:
            y:          Target, 1 = radiant win, 0 = dire win
            x1_hero:    2*N, heroes, 0:N radiant, N:2N dire, was hero present?
            x2_against: flattened upper triangular matrix hero, antagonist
                        interaction terms
            X3_all:     x1_hero + x2_against
        """

        if len(radiant_heroes) != len(dire_heroes):
            raise ValueError("Mismatch in number of matches radiant vs dire")

        num_matches = len(radiant_heroes)
        x1_hero = cls.first_order_vector(radiant_heroes, dire_heroes)

        # Second order effects
        x2_against = np.zeros([num_matches, int(meta.NUM_HEROES*(
                meta.NUM_HEROES-1)/2)], dtype=np.int8)

        # x_all = first order effects + match-ups ally vs. enemy
        x_all = np.zeros([num_matches, x1_hero.shape[1]+x2_against.shape[1]],
                         dtype=np.int8)

        counter = 0
        for rhs, dhs in zip(radiant_heroes, dire_heroes):
            x2_against[counter, :] = cls.flatten_second_order_upper(
                                    cls.second_order_hmatrix(rhs, dhs))
            x_all[counter, :] = np.concatenate([
                                    x1_hero[counter, :],
                                    x2_against[counter, :]
                                    ])

            if counter % 10000 == 0 and verbose:
                print("{} of {}".format(counter, num_matches))

            counter += 1

        return radiant_win, x1_hero, x2_against, x_all
