"""Deterministic, provisional translation proposals; never fit MFI or counts."""
from dataclasses import replace
import numpy as np
from scipy.signal import correlate
from .analysis import population_values
from .model import Ellipse, Polygon, Rectangle, ordered_gates
from .dependencies import gate_key


def ancestors(gates, population_id):
    by_id = {g.id:g for g in gates}
    ids = set()
    while population_id is not None:
        ids.add(population_id)
        population_id = by_id[population_id].parent_id
    return [g for g in ordered_gates(gates) if g.id in ids]


def translated(gate, shift):
    if isinstance(gate, Ellipse):
        return replace(gate, center=tuple(float(v+d) for v,d in zip(gate.center,shift)))
    if isinstance(gate, Polygon):
        return replace(gate, vertices=tuple(tuple(float(v+d) for v,d in zip(point,shift)) for point in gate.vertices))
    return replace(gate, minimum=tuple(float(v+d) for v,d in zip(gate.minimum,shift)),
                   maximum=tuple(float(v+d) for v,d in zip(gate.maximum,shift)))


def propose_translation(reference, source, reference_gates, current_gates, gate, cache,
                        *, bins=64, max_shift_fraction=.5, min_events=30, adapt_channels=()):
    diagnostics = {'method':'parent-density-translation-v1','flags':[], 'shift':[0.,0.],
                   'coordinate_scales':list(gate.scales), 'channels':list(gate.channels)}
    allowed = all(c.upper().startswith(('FSC-', 'SSC-')) or c in adapt_channels for c in gate.channels)
    closed = isinstance(gate, (Polygon, Ellipse)) or (isinstance(gate,Rectangle) and all(v is not None for v in gate.minimum+gate.maximum) and not gate.group_id)
    if not allowed or not closed or len(gate.channels)!=2:
        diagnostics['flags'] = ['Fixed geometry: adaptation requires a closed 2D gate and scatter channels, or explicit --adapt-channel authorization.']
        return gate, diagnostics, 'unresolved'
    identical = (reference.sha256==source.sha256
            and [gate_key(g) for g in ancestors(reference_gates,gate.parent_id)] ==
                [gate_key(g) for g in ancestors(current_gates,gate.parent_id)])
    ref = population_values(reference, ancestors(reference_gates,gate.id),gate.id,gate.channels,gate.scales,cache)
    target = population_values(source, ancestors(current_gates,gate.parent_id),gate.parent_id,gate.channels,gate.scales,cache)
    if isinstance(gate, Ellipse):
        reach = np.sqrt(np.square(np.asarray(gate.axes,dtype=float)).sum(axis=0))
        shape = np.asarray([np.asarray(gate.center)-reach, np.asarray(gate.center)+reach])
    else:
        shape = np.asarray(gate.vertices if isinstance(gate,Polygon) else (gate.minimum,gate.maximum),dtype=float)
    lo, hi = shape.min(axis=0), shape.max(axis=0)
    width = hi-lo
    if np.any(width<=0) or not np.isfinite(width).all():
        raise ValueError('Gate bounds cannot define a translation search window')
    edges = [np.linspace(a-w*max_shift_fraction,b+w*max_shift_fraction,bins+1) for a,b,w in zip(lo,hi,width)]
    step = np.array([e[1]-e[0] for e in edges])
    reference_hist, _, _ = np.histogram2d(ref[:,0],ref[:,1],bins=edges)
    target_hist, _, _ = np.histogram2d(target[:,0],target[:,1],bins=edges)
    diagnostics.update(reference_events=len(ref), target_parent_events=len(target),
                       target_window_events=int(target_hist.sum()), bin_width=step.tolist(),
                       search_limit=(width*max_shift_fraction).tolist())
    if len(ref)<min_events or target_hist.sum()<min_events:
        diagnostics['flags'] = [f'Insufficient population evidence (minimum {min_events} events).']
        return gate,diagnostics,'unresolved'
    if identical:
        diagnostics['identity_evidence']='Identical FCS bytes and ancestor definitions; no translation.'
        return gate,diagnostics,'proposed'
    norm=float(np.linalg.norm(reference_hist)*np.linalg.norm(target_hist))
    scores=correlate(target_hist,reference_hist,mode='full',method='fft')/norm
    lags=np.arange(-(bins-1),bins)
    xx,yy=np.meshgrid(lags,lags,indexing='ij')
    valid=(np.abs(xx*step[0])<=width[0]*max_shift_fraction)&(np.abs(yy*step[1])<=width[1]*max_shift_fraction)
    restricted=np.where(valid,scores,-np.inf)
    index=np.unravel_index(np.argmax(restricted),restricted.shape)
    lag=np.array([lags[index[0]],lags[index[1]]])
    shift=lag*step
    peak=float(scores[index])
    # Exclude the width of the reference correlation lobe when looking for a
    # competing alignment, rather than counting adjacent bins as another peak.
    radius=np.maximum(2,np.ceil(2*np.std(ref,axis=0)/step)).astype(int)
    alternative=valid & ((np.abs(xx-lag[0])>radius[0]) | (np.abs(yy-lag[1])>radius[1]))
    second=float(np.max(scores[alternative])) if np.any(alternative) else 0.
    ratio=max(0.,second)/peak if peak>0 else 1.
    diagnostics.update(estimated_shift=shift.tolist(), alignment_cosine=peak,
                       competing_peak_ratio=ratio, ambiguity_exclusion_bins=radius.tolist())
    if peak<.25:
        diagnostics['flags'].append('Weak density alignment (cosine < 0.25).')
    if ratio>=.9:
        diagnostics['flags'].append('Ambiguous population: competing alignment >= 90% of peak.')
    if np.any(np.abs(shift)+step>=width*max_shift_fraction):
        diagnostics['flags'].append('Best alignment reaches the configured search boundary.')
    if diagnostics['flags']:
        return gate,diagnostics,'unresolved'
    diagnostics['shift']=shift.tolist()
    return translated(gate,shift),diagnostics,'proposed'
