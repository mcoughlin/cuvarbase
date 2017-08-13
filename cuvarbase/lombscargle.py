import numpy as np
import pycuda.driver as cuda
import skcuda.fft as cufft
import pycuda.gpuarray as gpuarray
from pycuda.compiler import SourceModule
import resource
from .core import GPUAsyncProcess
from .utils import weights, find_kernel, _module_reader
from .utils import autofrequency as utils_autofreq
from .cunfft import NFFTAsyncProcess, nfft_adjoint_async, NFFTMemory

def get_k0(freqs):
    return max([1, int(round(freqs[0] / (freqs[1] - freqs[0])))])

def check_k0(freqs, k0=None, rtol=1E-2, atol=1E-7):
    k0 = k0 if k0 is not None else get_k0(freqs)
    df = freqs[1] - freqs[0]
    f0 = k0 * df
    assert(abs(f0 - freqs[0]) < rtol * df + atol)

class LombScargleMemory(object):
    def __init__(self, sigma, stream, m, **kwargs):

        self.sigma = sigma
        self.stream = stream
        self.m = m
        self.k0 = kwargs.get('k0', 0)
        self.precomp_psi = kwargs.get('precomp_psi', True)

        self.other_settings = {}
        self.other_settings.update(kwargs)

        self.n0 = kwargs.get('n0', None)
        self.nf = kwargs.get('nf', None)

        self.t_g = kwargs.get('t_g', None)
        self.yw_g = kwargs.get('yw_g', None)
        self.w_g = kwargs.get('w_g', None)
        self.lsp_g = kwargs.get('lsp_g', None)

        self.nfft_mem_yw = kwargs.get('nfft_mem_yw', None)
        self.nfft_mem_w = kwargs.get('nfft_mem_w', None)

        if self.nfft_mem_yw is None:
            self.nfft_mem_yw = NFFTMemory(self.sigma, self.stream,
                                          self.m, **kwargs)

        if self.nfft_mem_w is None:
            self.nfft_mem_w = NFFTMemory(self.sigma, self.stream,
                                         self.m, **kwargs)

        self.real_type = self.nfft_mem_yw.real_type
        self.complex_type = self.nfft_mem_yw.complex_type

        self.buffered_transfer = kwargs.get('buffered_transfer', False)
        self.n0_buffer = kwargs.get('n0_buffer', None)

        self.lsp_c = kwargs.get('lsp_c', None)

        self.t = kwargs.get('t', None)
        self.yw = kwargs.get('yw', None)
        self.w = kwargs.get('w', None)

    def allocate_data(self, **kwargs):
        n0 = kwargs.get('n0', self.n0)
        if self.buffered_transfer:
            n0 = kwargs.get('n0_buffer', self.n0_buffer)

        assert(n0 is not None)
        #print("allocating ", n0, " elements for each data array")
        self.t_g = gpuarray.zeros(int(n0), dtype=self.real_type)
        self.yw_g = gpuarray.zeros(int(n0), dtype=self.real_type)
        self.w_g = gpuarray.zeros(int(n0), dtype=self.real_type)

        self.nfft_mem_w.t_g = self.t_g
        self.nfft_mem_w.y_g = self.w_g

        self.nfft_mem_yw.t_g = self.t_g
        self.nfft_mem_yw.y_g = self.yw_g

        n0 = np.int32(n0)
        self.nfft_mem_yw.n0 = n0
        self.nfft_mem_w.n0 = n0

        return self

    def allocate_grids(self, **kwargs):
        k0 = kwargs.get('k0', self.k0)
        n0 = kwargs.get('n0', self.n0)
        if self.buffered_transfer:
            n0 = kwargs.get('n0_buffer', self.n0_buffer)
        assert(n0 is not None)

        self.nf = kwargs.get('nf', self.nf)
        assert(self.nf is not None)
        self.nf = np.int32(self.nf)

        if self.nfft_mem_yw.precomp_psi:
            self.nfft_mem_yw.allocate_precomp_psi(n0=n0)

        # Only one precomp psi needed
        self.nfft_mem_w.precomp_psi = False
        self.nfft_mem_w.q1 = self.nfft_mem_yw.q1
        self.nfft_mem_w.q2 = self.nfft_mem_yw.q2
        self.nfft_mem_w.q3 = self.nfft_mem_yw.q3

        self.nfft_mem_yw.allocate_grid(nf=self.nf)
        self.nfft_mem_w.allocate_grid(nf=2 * self.nf + k0)

        self.lsp_g = gpuarray.zeros(int(self.nf), dtype=self.real_type)
        return self

    def allocate_pinned_cpu(self, **kwargs):
        nf = kwargs.get('nf', self.nf)
        assert(nf is not None)

        self.lsp_c = cuda.aligned_zeros(shape=(nf,), dtype=self.real_type,
                                        alignment=resource.getpagesize())
        self.lsp_c = cuda.register_host_memory(self.lsp_c)

        return self

    def is_ready(self):
        raise NotImplementedError()

    def allocate_buffered_data_arrays(self, **kwargs):
        n0 = kwargs.get('n0', self.n0)
        if self.buffered_transfer:
            n0 = kwargs.get('n0_buffer', self.n0_buffer)
        assert(n0 is not None)

        self.t = cuda.aligned_zeros(shape=(n0,),
                                    dtype=self.real_type,
                                    alignment=resource.getpagesize())
        self.t = cuda.register_host_memory(self.t)

        self.yw = cuda.aligned_zeros(shape=(n0,),
                                     dtype=self.real_type,
                                     alignment=resource.getpagesize())
        self.yw = cuda.register_host_memory(self.yw)

        self.w = cuda.aligned_zeros(shape=(n0,),
                                    dtype=self.real_type,
                                    alignment=resource.getpagesize())
        self.w = cuda.register_host_memory(self.w)
        return self

    def allocate(self, **kwargs):
        self.nf = kwargs.get('nf', self.nf)
        assert(self.nf is not None)

        self.nf = np.int32(self.nf)

        self.allocate_data(**kwargs)
        self.allocate_grids(**kwargs)
        self.allocate_pinned_cpu(**kwargs)

        if self.buffered_transfer:
            self.allocate_buffered_data_arrays(**kwargs)

        return self

    def setdata(self, **kwargs):
        t = kwargs.get('t', self.t)
        yw = kwargs.get('yw', self.yw)
        w = kwargs.get('w', self.w)

        y = kwargs.get('y', None)
        dy = kwargs.get('dy', None)
        self.ybar = self.real_type(0)
        self.yy = self.real_type(kwargs.get('yy', 1.))

        self.n0 = kwargs.get('n0', np.int32(len(t)))
        if self.n0 is not None:
            self.n0 = np.int32(self.n0)
        if dy is not None:
            assert('w' not in kwargs)
            w = weights(dy)

        if y is not None:
            assert('yw' not in kwargs)

            self.ybar = self.real_type(np.dot(y, w))
            yw = np.multiply(w, y - self.ybar)
            y2 = np.power(y - self.ybar, 2)
            self.yy = self.real_type(np.dot(w, y2))

        t = np.asarray(t).astype(self.real_type)
        yw = np.asarray(yw).astype(self.real_type)
        w = np.asarray(w).astype(self.real_type)

        if self.buffered_transfer:
            if any([arr is None for arr in [self.t, self.yw, self.w]]):
                if self.buffered_transfer:
                    self.allocate_buffered_data_arrays(**kwargs)

            assert(self.n0 <= len(self.t))

            self.t[:self.n0] = t[:self.n0]
            self.yw[:self.n0] = yw[:self.n0]
            self.w[:self.n0] = w[:self.n0]
        else:
            self.t = np.asarray(t).astype(self.real_type)
            self.yw = np.asarray(yw).astype(self.real_type)
            self.w = np.asarray(w).astype(self.real_type)

        # Set minimum and maximum t values (needed to scale things
        # for the NFFT)
        self.tmin = self.real_type(min(t))
        self.tmax = self.real_type(max(t))

        self.nfft_mem_yw.tmin = self.tmin
        self.nfft_mem_w.tmin = self.tmin

        self.nfft_mem_yw.tmax = self.tmax
        self.nfft_mem_w.tmax = self.tmax

        self.nfft_mem_w.n0 = np.int32(len(t))
        self.nfft_mem_yw.n0 = np.int32(len(t))

        return self

    def transfer_data_to_gpu(self, **kwargs):
        t, yw, w = self.t, self.yw, self.w

        assert(not any([arr is None for arr in [t, yw, w]]))

        # Do asynchronous data transfer
        #print(len(t), len(self.t_g))
        self.t_g.set_async(t, stream=self.stream)
        self.yw_g.set_async(yw, stream=self.stream)
        self.w_g.set_async(w, stream=self.stream)

    def transfer_lsp_to_cpu(self, **kwargs):
        #print(self.lsp_c, self.lsp_g.ptr, self.stream)
        cuda.memcpy_dtoh_async(self.lsp_c, self.lsp_g.ptr,
                               stream=self.stream)

    def fromdata(self, **kwargs):
        self.setdata(**kwargs)

        if kwargs.get('allocate', True):
            self.allocate(**kwargs)

        return self

    def set_gpu_arrays_to_zero(self, **kwargs):
        for x in [self.t_g, self.yw_g, self.w_g]:
            if not x is None:
                x.fill(self.real_type(0))

        for x in [self.t, self.yw, self.w]:
            if not x is None:
                x[:] = self.real_type(0.)

        for mem in [self.nfft_mem_yw, self.nfft_mem_w]:
            mem.ghat_g.fill(self.real_type(0))


def lomb_scargle_direct_sums(t, yw, w, freqs, YY):
    """
    Super slow lomb scargle (for debugging), uses
    direct summations to compute C, S, ...
    """
    lsp = np.zeros(len(freqs))
    for i, freq in enumerate(freqs):
        if abs(freq) < 1E-9:
            lsp[i] = 0.
        phase = 2 * np.pi * (((t + 0.5) * freq) % 1.0).astype(np.float64)
        phase2 = 2 * np.pi * (((t + 0.5) * 2 * freq) % 1.0).astype(np.float64)

        cc = np.cos(phase)
        ss = np.sin(phase)

        cc2 = np.cos(phase2)
        ss2 = np.sin(phase2)

        C = np.dot(w, cc)
        S = np.dot(w, ss)

        C2 = np.dot(w, cc2)
        S2 = np.dot(w, ss2)

        Ch = np.dot(yw, cc)
        Sh = np.dot(yw, ss)

        tan_2omega_tau = (S2 - 2 * S * C) / (C2 - (C * C - S * S))

        C2w = 1. / np.sqrt(1 + tan_2omega_tau * tan_2omega_tau)
        S2w = tan_2omega_tau * C2w

        Cw = np.sqrt(0.5 * (1 + C2w))
        Sw = np.sqrt(0.5 * (1 - C2w))

        if S2w < 0:
            Sw *= -1

        YC = Ch * Cw + Sh * Sw
        YS = Sh * Cw - Ch * Sw
        CC = 0.5 * (1 + C2 * C2w + S2 * S2w)
        SS = 0.5 * (1 - C2 * C2w - S2 * S2w)

        CC -= (C * Cw + S * Sw) * (C * Cw + S * Sw)
        SS -= (S * Cw - C * Sw) * (S * Cw - C * Sw)

        P = (YC * YC / CC + YS * YS / SS) / YY

        if (np.isinf(P) or np.isnan(P) or P < 0 or P > 1):
            print(P, YC, YS, CC, SS, YY)
            P = 0.
        lsp[i] = P

    return lsp


def lomb_scargle_async(memory, functions, freqs,
                       block_size=256, use_fft=True,
                       python_dir_sums=False,
                       transfer_to_device=True,
                       transfer_to_host=True, **kwargs):
    """
    Asynchronous Lomb Scargle periodogram

    Use the ``LombScargleAsyncProcess`` class and
    related subroutines when possible.

    Parameters
    ----------
    memory: ``LombScargleMemory``
        Allocated memory, must have data already set (see, e.g.,
        ``LombScargleAsyncProcess.allocate()``)
    functions: tuple (lombscargle_functions, nfft_functions)
        Tuple of compiled functions from ``SourceModule``. Must be
        prepared with their appropriate dtype.
    freqs: array_like, optional (default: 0)
        Linearly-spaced frequencies starting at an integer multiple
        of the frequency spacing (i.e. freqs = df * (k0 + np.arange(nf)))
    block_size: int, optional
        Number of CUDA threads per block
    use_fft: bool, optional (default: True)
        If False, uses direct sums.
    python_dir_sums: bool, optional (default: False)
        If True, performs direct sums with Python on the CPU
    transfer_to_device: bool, optional, (default: True)
        If the data is already on the gpu, set as False
    transfer_to_host: bool, optional, (default: True)
        If False, will not transfer the resulting periodogram to
        CPU memory

    Returns
    -------
    lsp_c: ``np.array``
        The resulting periodgram (``memory.lsp_c``)
    """
    (lomb, lomb_dirsum), nfft_funcs = functions


    df = freqs[1] - freqs[0]
    samples_per_peak = 1./((memory.tmax - memory.tmin) * df)
    assert(get_k0(freqs) == memory.k0)

    stream = memory.stream

    block = (block_size, 1, 1)
    grid = (int(np.ceil(memory.nf / float(block_size))), 1)

    # lightcurve -> gpu
    if transfer_to_device:
        memory.transfer_data_to_gpu()

    #print(memory.t_g.get())
    # do direct summations with python on the CPU (for debugging)
    if python_dir_sums:
        t = memory.t_g.get()
        yw = memory.yw_g.get()
        w = memory.w_g.get()
        return lomb_scargle_direct_sums(t, yw, w, freqs, memory.yy)

    # Use direct sums (on GPU)
    if not use_fft:
        args = (grid, block, stream,
                memory.t_g.ptr, memory.yw_g.ptr, memory.w_g.ptr,
                memory.lsp_g.ptr, memory.nf, memory.n0, memory.yy,
                df, memory.real_type(min(freqs)))

        lomb_dirsum.prepared_async_call(*args)
        if transfer_to_device:
            memory.transfer_lsp_to_cpu()
        return memory.lsp_c
    else:
        # NFFT
        nfft_kwargs = dict(transfer_to_host=False,
                           transfer_to_device=False)

        nfft_kwargs.update(kwargs)

        nfft_kwargs['minimum_frequency'] = freqs[0]
        nfft_kwargs['samples_per_peak'] = samples_per_peak

        # NFFT(w * (y - ybar))
        nfft_adjoint_async(memory.nfft_mem_yw, nfft_funcs, **nfft_kwargs)

        # NFFT(w)
        nfft_adjoint_async(memory.nfft_mem_w, nfft_funcs, **nfft_kwargs)


    args = (grid, block, stream)
    args += (memory.nfft_mem_w.ghat_g.ptr, memory.nfft_mem_yw.ghat_g.ptr)
    args += (memory.lsp_g.ptr, memory.nf, memory.yy, memory.k0)
    lomb.prepared_async_call(*args)

    if transfer_to_host:
        memory.transfer_lsp_to_cpu()

    return memory.lsp_c


class LombScargleAsyncProcess(GPUAsyncProcess):
    """
    GPUAsyncProcess for the Lomb Scargle periodogram

    Parameters
    ----------
    **kwargs: passed to ``NFFTAsyncProcess``

    Example
    -------
    >>> proc = LombScargleAsyncProcess()
    >>> Ndata = 1000
    >>> t = np.sort(365 * np.random.rand(N))
    >>> y = 12 + 0.01 * np.cos(2 * np.pi * t / 5.0)
    >>> y += 0.01 * np.random.randn(len(t))
    >>> dy = 0.01 * np.ones_like(y)
    >>> freqs, powers = proc.run([(t, y, dy)])
    >>> proc.finish()
    >>> ls_freqs, ls_powers = freqs[0], powers[0]

    """
    def __init__(self, *args, **kwargs):
        super(LombScargleAsyncProcess, self).__init__(*args, **kwargs)

        self.nfft_proc = NFFTAsyncProcess(*args, **kwargs)
        self._cpp_defs = self.nfft_proc._cpp_defs

        self.real_type = self.nfft_proc.real_type
        self.complex_type = self.nfft_proc.complex_type

        self.block_size = self.nfft_proc.block_size
        self.module_options = self.nfft_proc.module_options
        self.use_double = self.nfft_proc.use_double

    def _compile_and_prepare_functions(self, **kwargs):

        module_text = _module_reader(find_kernel('lomb'), self._cpp_defs)

        self.module = SourceModule(module_text, options=self.module_options)
        self.dtypes = dict(
            lomb=[np.intp, np.intp, np.intp, np.int32,
                  self.real_type, np.int32],
            lomb_dirsum=[np.intp, np.intp, np.intp, np.intp,
                         np.int32, np.int32, self.real_type, self.real_type,
                         self.real_type]
        )

        self.nfft_proc._compile_and_prepare_functions(**kwargs)
        for fname, dtype in self.dtypes.iteritems():
            func = self.module.get_function(fname)
            self.prepared_functions[fname] = func.prepare(dtype)
        self.function_tuple = tuple(self.prepared_functions[fname]
                                    for fname in sorted(self.dtypes.keys()))

    def memory_requirement(self, data, **kwargs):
        """ return an approximate GPU memory requirement in bytes """
        raise NotImplementedError()

    def allocate_for_single_lc(self, t, y, dy, nf, k0=0,
                               stream=None, **kwargs):
        """
        Allocate GPU (and possibly CPU) memory for single lightcurve

        Parameters
        ----------
        t: array_like
            Observation times
        y: array_like
            Observations
        dy: array_like
            Observation uncertainties
        nf: int
            Number of frequencies
        k0: int
            The starting index for the Fourier transform. The minimum
            frequency ``f0 = k0 * df``, where ``df`` is the frequency
            spacing
        stream: pycuda.driver.Stream
            CUDA stream you want this to run on
        **kwargs

        Returns
        -------
        mem: LombScargleMemory
            Memory object.
        """
        m = self.nfft_proc.get_m(nf)

        sigma = self.nfft_proc.sigma

        mem = LombScargleMemory(sigma, stream, m, k0=k0, **kwargs)

        mem.fromdata(t=t, y=y, dy=dy, nf=nf, allocate=True, **kwargs)

        return mem

    def autofrequency(self, *args, **kwargs):
        return utils_autofreq(*args, **kwargs)

    def _nfreqs(self, *args, **kwargs):

        return len(self.autofrequency(*args, **kwargs))

    def allocate(self, data, nfreqs=None, k0s=None, **kwargs):

        """
        Allocate GPU memory for Lomb Scargle computations

        Parameters
        ----------
        data: list of (t, y, N) tuples
            List of data, ``[(t_1, y_1, w_1), ...]``
            * ``t``: Observation times
            * ``y``: Observations
            * ``w``: Observation **weights** (sum(w) = 1)
        **kwargs

        Returns
        -------
        allocated_memory: list of ``LombScargleMemory``
            list of allocated memory objects for each lightcurve

        """

        if len(data) > len(self.streams):
            self._create_streams(len(data) - len(self.streams))

        allocated_memory = []

        nfrqs = nfreqs
        k0 = k0s
        if nfrqs is None:
            nfrqs = [self._nfreqs(t, **kwargs) for (t, y, dy) in data]
        elif isinstance(nfreqs, int):
            nfrqs = nfrqs * np.ones(len(data))

        if k0s is None:
            k0s = [1] * len(nfrqs)
        elif isinstance(k0s, float):
            k0s = [k0s] * len(nfrqs)
        for i, ((t, y, dy), nf, k0) in enumerate(zip(data, nfrqs, k0s)):
            mem = self.allocate_for_single_lc(t, y, dy, nf, k0=k0,
                                              stream=self.streams[i],
                                              **kwargs)
            allocated_memory.append(mem)

        return allocated_memory

    def run(self, data,
            use_fft=True, memory=None,
            freqs=None,
            **kwargs):

        """
        Run Lomb Scargle on a batch of data.

        Parameters
        ----------
        data: list of tuples
            list of [(t, y, dy), ...] containing
            * ``t``: observation times
            * ``y``: observations
            * ``dy``: observation uncertainties
        freqs: optional, list of ``np.ndarray`` frequencies
            List of custom frequencies. Right now, this has to be linearly
            spaced with ``freqs[0] / (freqs[1] - freqs[0])`` being an integer.
        memory: optional, list of ``LombScargleMemory`` objects
            List of memory objects, length of list must be ``>= len(data)``
        use_fft: optional, bool (default: True)
            Uses the NFFT, otherwise just does direct summations (which
            are quite slow...)
        **kwargs

        Returns
        -------
        results: list of lists
            list of (freqs, pows) for each LS periodogram

        """

        # compile module if not compiled already
        if not hasattr(self, 'prepared_functions') or \
            not all([func in self.prepared_functions for func in
                     ['lomb', 'lomb_dirsum']]):
            self._compile_and_prepare_functions(**kwargs)

        # create and/or check frequencies
        frqs = freqs
        if frqs is None:
            frqs = [self.autofrequency(d[0], **kwargs) for d in data]

        elif isinstance(frqs[0], float):
            frqs = [frqs] * len(data)

        assert(len(frqs) == len(data))

        dfs = [frq[1] - frq[0] for frq in frqs]
        k0s = [get_k0(frq) for frq in frqs]

        # make sure k0 * df is the minimum frequency
        [check_k0(frq, k0=k0) for frq, k0 in zip(frqs, k0s)]

        if memory is None:
            nfreqs = [len(frq) for frq in frqs]
            memory = self.allocate(data, nfreqs=nfreqs, k0s=k0s,
                                    use_double=self.use_double,
                                    **kwargs)
        else:
            for i, (t, y, dy) in enumerate(data):
                memory[i].set_gpu_arrays_to_zero(**kwargs)
                memory[i].setdata(t=t, y=y, dy=dy, **kwargs)

        ls_kwargs = dict(block_size=self.block_size,
                         use_fft=use_fft)
        ls_kwargs.update(kwargs)

        funcs = (self.function_tuple, self.nfft_proc.function_tuple)
        results = [lomb_scargle_async(memory[i], funcs, frqs[i],
                                      **ls_kwargs)
                   for i in range(len(data))]

        results = [(f, r) for f, r in zip(frqs, results)]
        return results

    def batched_run_const_nfreq(self, data, batch_size=10,
                                use_fft=True, freqs=None,
                                **kwargs):
        """
        Same as ``batched_run`` but is more efficient when the frequencies are
        the same for each lightcurve. Doesn't reallocate memory for each batch.

        Notes
        -----
        To get best efficiency, make sure the maximum number of observations
        is not much larger than the typical number of observations
        """

        # compile and prepare module functions if not already done
        if not hasattr(self, 'prepared_functions') or \
            not all([func in self.prepared_functions for func in
                     ['lomb', 'lomb_dirsum']]):
            self._compile_and_prepare_functions(**kwargs)

        # create streams if needed
        bsize = min([len(data), batch_size])
        if len(self.streams) < bsize:
            self._create_streams(bsize - len(self.streams))

        streams = [self.streams[i] for i in range(bsize)]
        max_ndata = max([len(t) for t, y, dy in data])

        if freqs is None:
            data_with_max_baseline = max(data, key=lambda d : max(d[0]) - min(d[0]))
            freqs = self.autofrequency(data_with_max_baseline[0], **kwargs)

            # now correct frequencies
            df = freqs[1] - freqs[0]
            k0 = get_k0(freqs)
            #nf = len(freqs)
            nf = int(round(max(freqs) / df)) - k0
            freqs = df * (k0 + np.arange(nf))

        df = freqs[1] - freqs[0]
        k0 = get_k0(freqs)
        nf = len(freqs)
        check_k0(freqs, k0=k0)

        lsps = []

        # make data batches
        batches = []
        while len(batches) * batch_size < len(data):
            start = len(batches) * batch_size
            finish = start + min([batch_size, len(data) - start])
            batches.append([data[i] for i in range(start, finish)])

        # set up memory containers for gpu and cpu (pinned) memory
        m = self.nfft_proc.get_m(nf)
        sigma = self.nfft_proc.sigma

        kwargs_lsmem = dict(buffered_transfer=True,
                            n0_buffer=max_ndata,
                            use_double=self.use_double)
        kwargs_lsmem.update(kwargs)
        memory = [LombScargleMemory(sigma, stream, m, k0=k0,
                                    **kwargs_lsmem)
                  for stream in streams]

        # allocate memory
        [mem.allocate(nf=nf, **kwargs) for mem in memory]

        funcs = (self.function_tuple, self.nfft_proc.function_tuple)
        for b, batch in enumerate(batches):
            results = self.run(batch, memory=memory, freqs=freqs,
                               use_fft=use_fft,
                               **kwargs)
            self.finish()

            for i, (f, p) in enumerate(results):
                lsps.append(np.copy(p))

        return [(freqs, lsp) for lsp in lsps]


def lomb_scargle_simple(t, y, dy, **kwargs):
    """
    Simple lomb-scargle interface for testing that
    things work on the GPU. Note: This will be
    substantially slower than working with the
    ``LombScargleAsyncProcess`` interface.
    """

    w = np.power(dy, -2)
    w /= sum(w)
    proc = LombScargleAsyncProcess()
    results = proc.run([(t, y, w)], **kwargs)

    freqs, powers = results[0]

    proc.finish()

    return freqs, powers
