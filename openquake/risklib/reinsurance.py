# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2022, GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import pandas as pd
import numpy as np
from openquake.baselib.general import BASE183, fast_agg2
from openquake.baselib.performance import compile, Monitor
from openquake.baselib.parallel import Starmap, pack, unpack, unlink
from openquake.baselib.writers import scientificformat
from openquake.hazardlib import nrml, InvalidFile
from openquake.risklib import scientific
"""
Here is some info about the used data structures.
There are 3 main dataframes:

1. treaty_df (id, type, deductible, limit, code)
   with type in prop, wxlr, catxl
2. policy_df (policy, liability, deductible, prop1, nonprop1, cat1)
3. risk_by_event (event_id, agg_id, loss) with agg_id == policy_id-1
"""
NOLIMIT = 1E100
KNOWN_LOSS_TYPES = {
    'structural', 'nonstructural', 'contents',
    'value-structural', 'value-nonstructural', 'value-contents'}
DEBUG = False


def check_fields(fields, dframe, idxdict, fname):
    """
    :param fields: fields to check (the first field is the primary key)
    :param dframe: DataFrame with the contents of fname
    :param idxdict: dictionary key -> index (starting from 1)
    :param fname: file containing the fields to check
    """
    key = fields[0]
    idx = [idxdict[name] for name in dframe[key]]  # indices starting from 1
    dframe[key] = idx
    for no, field in enumerate(fields):
        if field not in dframe.columns:
            raise InvalidFile(f'{fname}: {field} is missing in the header')


# validate the file policy.csv
def check_fractions(colnames, colvalues, fname):
    """
    Make sure the sum of the proportional fractions is below 1 and raise
    a clear error if not.
    """
    n = len(colvalues[0])
    for i in range(n):
        tot = 0
        for c, col in enumerate(colnames):
            frac = colvalues[c][i]
            if frac > 1 or frac < 0:
                raise ValueError(
                    f'{fname}:{i+2}: invalid fraction {col}={frac}')
            tot += frac
        if tot > 1:
            raise ValueError(f'{fname}:{i+2} the sum of the fractions must be '
                             f'under 1, got {tot}')


def parse(fname, policy_idx):
    """
    :param fname: CSV file containing the policies
    :param policy_idx: dictionary policy name -> policy index

    Parse a reinsurance.xml file and returns
    (policy_df, treaty_df, field_map)
    """
    rmodel = nrml.read(fname).reinsuranceModel
    fieldmap = {}
    fmap = {}  # ex: {'deductible': 'Deductible', 'liability': 'Limit'}
    treaty = dict(id=[], type=[], deductible=[], limit=[])
    nonprop = set()
    colnames = []
    for node in rmodel.fieldMap:
        col = node.get('oq')
        if col:
            fmap[col] = node['input']
        if col in ('policy', 'deductible', 'liability'):  # not a treaty
            fieldmap[node['input']] = col
            continue
        treaty_type = node.get('type', 'prop')
        assert treaty_type in ('prop', 'wxlr', 'catxl'), treaty_type
        if treaty_type == 'prop':
            limit = node.get('max_cession_event', NOLIMIT)
            deduc = 0
            colnames.append(node['input'])
        else:
            limit = node['limit']
            deduc = node['deductible']
            nonprop.add(node['input'])
        treaty['id'].append(node['input'])
        treaty['type'].append(treaty_type)
        treaty['deductible'].append(deduc)
        treaty['limit'].append(limit)
    policyfname = os.path.join(os.path.dirname(fname), ~rmodel.policies)
    df = pd.read_csv(policyfname, keep_default_na=False).rename(
        columns=fieldmap)
    check_fields(['policy', 'deductible', 'liability'], df, policy_idx, fname)

    # validate policy input
    for col in nonprop:
        df[col] = np.bool_(df[col])
    if colnames:
        colvalues = [df[col].to_numpy() for col in colnames]
        check_fractions(colnames, colvalues, policyfname)
    treaty_df = pd.DataFrame(treaty)
    treaty_df['code'] = [BASE183[i] for i in range(len(treaty_df))]
    missing_treaties = set(df.columns) - set(treaty_df.id) - {
        'policy', 'deductible', 'liability'}
    for col in missing_treaties:  # remove missing treaties
        del df[col]
    return df, treaty_df, fmap


@compile(["(float64[:],float64[:],float64,float64)",
          "(float64[:],float32[:],float64,float64)",
          "(float32[:],float32[:],float64,float64)"])
def apply_treaty(cession, retention, deduc, capacity):
    for i, ret in np.ndenumerate(retention):
        overmax = ret - deduc
        if ret > deduc:
            if overmax > capacity:
                retention[i] = deduc + overmax - capacity
                cession[i] = capacity
            else:
                retention[i] = deduc
                cession[i] = overmax


def claim_to_cessions(claim, policy, treaty_df):
    """
    :param claim: an array of claims
    :param policy: a dictionary corresponding to a specific policy
    :param treaty_df: dataframe with treaties

    Converts an array of claims into a dictionary of arrays.
    """
    # proportional cessions
    cols = treaty_df[treaty_df.type == 'prop'].id
    fractions = [policy[col] for col in cols]
    assert sum(fractions) <= 1
    out = {'retention': claim * (1. - sum(fractions)), 'claim': claim}
    for col, frac in zip(cols, fractions):
        out[col] = claim * frac

    # wxlr cessions, totally independent from the overspill
    wxl = treaty_df[treaty_df.type == 'wxlr']
    for col, deduc, limit in zip(wxl.id, wxl.deductible, wxl.limit):
        out[col] = np.zeros(len(claim))
        if policy[col]:
            apply_treaty(out[col], out['retention'], deduc, limit - deduc)

    return {k: np.round(v, 6) for k, v in out.items()}


def build_policy_grp(policy, treaty_df):
    """
    :param policy: policy dictionary or record
    :param treaty_df: treaty DataFrame
    :returns: the policy_grp for the given policy
    """
    cols = treaty_df.id.to_numpy()
    codes = treaty_df.code.to_numpy()
    types = treaty_df.type.to_numpy()
    key = list(codes)
    for c, col in enumerate(cols):
        if types[c] == 'catxl' and policy[col] == 0:
            key[c] = '.'
    return ''.join(key)


def line(row, fmt='%d'):
    return ''.join(scientificformat(val, fmt).rjust(11) for val in row)


def clever_agg(ukeys, datalist, treaty_df, idx, overdict, eids):
    """
    :param ukeys: a list of unique keys
    :param datalist: a list of matrices of the shape (E, 2+T)
    :param treaty_df: a treaty DataFrame
    :param idx: a dictionary treaty.code -> cession index
    :param overdic: a dictionary treaty.code -> overspill array

    Recursively compute cessions and retentions for each treaty.
    Populate the cession dictionary and returns the final retention.
    """
    if DEBUG:
        print()
        print(line(['event_id', 'policy_grp'] + list(idx)))
        rows = []
        for key, data in zip(ukeys, datalist):
            # printing the losses
            for eid, row in zip(eids, data):
                rows.append([eid, key] + list(row))
        for row in sorted(rows):
            print(line(row))
    if len(ukeys) == 1 and ukeys[0] == '':
        return datalist[0]
    newkeys, newdatalist = [], []
    for key, data in zip(ukeys, datalist):
        code = key[0]
        newkey = key[1:]
        if code != '.':
            tr = treaty_df.loc[code]
            ret = data[:, idx['retention']]
            cession = data[:, idx[code]]
            capacity = tr.limit - tr.deductible
            has_over = False
            if tr.type == 'catxl':
                overspill = ret - tr.deductible - capacity
                has_over = (overspill > 0).any()
                apply_treaty(cession, ret, tr.deductible, capacity)
            elif tr.type == 'prop':
                overspill = cession - capacity
                over = overspill > 0
                has_over = (overspill > 0).any()
                if has_over:
                    ret[over] += cession[over] - tr.limit
                    cession[over] = tr.limit
            if has_over:
                overdict['over_' + code] = np.maximum(overspill, 0)
        newkeys.append(newkey)
        newdatalist.append(data)
    keys, sums = fast_agg2(newkeys, np.array(newdatalist))
    return clever_agg(keys, sums, treaty_df, idx, overdict, eids)


def group_by_policies(policy_dicts, sharedf, treaty_df, monitor):
    agglosses_df = unpack(sharedf)
    dfs = []
    for pol in policy_dicts:
        dfs.append(by_policy(agglosses_df, pol, treaty_df))
    return pd.concat(dfs)


# tested in test_reinsurance.py
def by_policy(agglosses_df, pol_dict, treaty_df):
    '''
    :param DataFrame agglosses_df:
        losses aggregated by policy (keys agg_id, event_id)
    :param dict pol_dict:
        Policy parameters, with pol_dict['policy'] being an integer >= 1
    :param DataFrame treaty_df:
        All treaties
    :returns:
        DataFrame of reinsurance losses by event ID and policy ID
    '''
    out = {}
    df = agglosses_df[agglosses_df.agg_id == pol_dict['policy'] - 1]
    losses = df.loss.to_numpy()
    ded, lim = pol_dict['deductible'], pol_dict['liability']
    claim = scientific.insured_losses(losses, ded, lim)
    out['event_id'] = df.event_id.to_numpy()
    out['policy_id'] = np.array([pol_dict['policy']] * len(df), int)
    out.update(claim_to_cessions(claim, pol_dict, treaty_df))
    nonzero = out['claim'] > 0  # discard zero claims
    out_df = pd.DataFrame({k: out[k][nonzero] for k in out})
    out_df['policy_grp'] = build_policy_grp(pol_dict, treaty_df)
    return out_df


def _by_event(rbp, treaty_df, mon=Monitor()):
    with mon('processing policy_loss_table', measuremem=True):
        tdf = treaty_df.set_index('code')
        inpcols = ['eid', 'claim'] + [t.id for _, t in tdf.iterrows()
                                      if t.type != 'catxl']
        outcols = ['retention', 'claim'] + list(tdf.index)
        idx = {col: i for i, col in enumerate(outcols)}
        eids, idxs = np.unique(rbp.event_id.to_numpy(), return_inverse=True)
        rbp['eid'] = idxs
        E = len(eids)
        dic = dict(event_id=eids)
        keys, datalist = [], []
        for key, grp in rbp.groupby('policy_grp'):
            logging.info('Processing policy group %r with %d rows',
                         key, len(grp))
            data = np.zeros((E, len(outcols)))
            gb = grp[inpcols].groupby('eid').sum()
            for i, col in enumerate(inpcols):
                if i > 0:  # claim, noncat1, ...
                    data[gb.index, i] = gb[col].to_numpy()
            data[:, 0] = data[:, 1]  # retention = claim - noncats
            for c in range(2, len(outcols)):
                data[:, 0] -= data[:, c]
            keys.append(key)
            datalist.append(data)
        del rbp['eid']
    with mon('reinsurance by event', measuremem=True):
        overspill = {}
        res = clever_agg(keys, datalist, tdf, idx, overspill, eids)

        # sanity check on the result
        ret = res[:, 0]
        claim = res[:, 1]
        cession = res[:, 2:].sum(axis=1)
        np.testing.assert_allclose(cession + ret, claim)

        dic.update({col: res[:, c] for c, col in enumerate(outcols)})
        dic.update(overspill)
        alias = dict(zip(tdf.index, tdf.id))
        df = pd.DataFrame(dic).rename(columns=alias)
    return df


def by_policy_event(agglosses_df, policy_df, treaty_df, mon=Monitor()):
    """
    :param DataFrame agglosses_df: losses aggregated by (agg_id, event_id)
    :param DataFrame policy_df: policies
    :param DataFrame treaty_df: treaties
    :returns: (risk_by_policy_df, risk_by_event_df)
    """
    logging.info("Processing %d policies", len(policy_df))
    try:
        sharedf = pack(agglosses_df)
        policies = [dict(pol) for _, pol in policy_df.iterrows()]
        smap = Starmap.apply(group_by_policies,
                             (policies, sharedf, treaty_df))
        rbp = pd.concat(list(smap))
    finally:
        unlink(sharedf)
    if DEBUG:
        print(rbp.sort_values('event_id'))
    rbe = _by_event(rbp, treaty_df, mon)
    del rbp['policy_grp']
    return rbp, rbe
