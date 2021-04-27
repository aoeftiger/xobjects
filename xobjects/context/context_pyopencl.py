import os
import logging

import numpy as np

from .general import XBuffer, XContext, ModuleNotAvailable, available
from .specialize_source import specialize_source

try:
    import pyopencl as cl
    import pyopencl.array as cla

    _enabled = True
except ImportError:
    print(
        "WARNING: pyopencl is not installed, this context will not be available"
    )
    cl = ModuleNotAvailable(
        message=(
            "pyopencl is not installed. " "this context is not available!"
        )
    )
    cl.Buffer = cl
    cla = cl
    _enabled = False

from ._patch_pyopencl_array import _patch_pyopencl_array

log = logging.getLogger(__name__)


class ContextPyopencl(XContext):
    @classmethod
    def print_devices(cls):
        for ip, platform in enumerate(cl.get_platforms()):
            print(f"Context {ip}: {platform.name}")
            for id, device in enumerate(platform.get_devices()):
                print(f"Device {ip}.{id}: {device.name}")

    def __init__(self, device=None, patch_pyopencl_array=True):

        """
        Creates a Pyopencl Context object, that allows performing the computations
        on GPUs and CPUs through PyOpenCL.

        Args:
            device (str or Device): The device (CPU or GPU) for the simulation.
            default_kernels (bool): If ``True``, the Xfields defult kernels are
                automatically imported.
            patch_pyopencl_array (bool): If ``True``, the PyOpecCL class is patched to
                allow some operations with non-contiguous arrays.
            specialize_code (bool): If True, the code is specialized using
                annotations in the source code. Default is ``True``

        Returns:
            ContextPyopencl: context object.

        """

        super().__init__()

        # TODO assume one device only
        if device is None:
            self.context = cl.create_some_context(interactive=False)
            self.device = self.context.devices[0]
            self.platform = self.device.platform
        else:
            if isinstance(device, str):
                platform, device = map(int, device.split("."))
            else:
                self.device = device
                self.platform = device.platform

            self.platform = cl.get_platforms()[platform]
            self.device = self.platform.get_devices()[device]
            self.context = cl.Context([self.device])

        self.queue = cl.CommandQueue(self.context)

        if patch_pyopencl_array:
            _patch_pyopencl_array(cl, cla, self.context)

    def _make_buffer(self, capacity):
        return BufferPyopencl(capacity=capacity, context=self)

    def add_kernels(
        self,
        sources,
        kernels,
        specialize=True,
        save_source_as=None,
    ):

        source = []
        fold_list = set()
        for ss in sources:
            if hasattr(ss, "read"):
                source.append(ss.read())
                fold_list.add(os.path.dirname(ss.name))
            else:
                source.append(ss)
        source = "\n".join(source)

        if specialize:
            # included files are searched in the same folders od the src_filed
            source = specialize_source(
                source, specialize_for="opencl", search_in_folders=fold_list
            )

        if save_source_as is not None:
            with open(save_source_as, "w") as fid:
                fid.write(source)

        prg = cl.Program(self.context, source).build()

        for pyname, kernel in kernels.items():
            if kernel.c_name is None:
                kernel.c_name = pyname

            self.kernels[pyname] = KernelPyopencl(
                function=getattr(prg, kernel.c_name),
                description=kernel,
                context=self
            )

    def nparray_to_context_array(self, arr):

        """
        Copies a numpy array to the device memory.
        Args:
            arr (numpy.ndarray): Array to be transferred

        Returns:
            pyopencl.array.Array:The same array copied to the device.

        """
        dev_arr = cla.to_device(self.queue, arr)
        return dev_arr

    def nparray_from_context_array(self, dev_arr):

        """
        Copies an array to the device to a numpy array.

        Args:
            dev_arr (pyopencl.array.Array): Array to be transferred.
        Returns:
            numpy.ndarray: The same data copied to a numpy array.

        """
        return dev_arr.get()

    @property
    def nplike_lib(self):
        """
        Module containing all the numpy features supported by PyOpenCL (optionally
        with patches to operate with non-contiguous arrays).
        """
        return cla

    def synchronize(self):
        """
        Ensures that all computations submitted to the context are completed.
        No action is performed by this function in the Pyopencl context. The method
        is provided so that the Pyopencl context has an identical API to the Cupy one.
        """
        pass

    def zeros(self, *args, **kwargs):
        """
        Allocates an array of zeros on the device. The function has the same
        interface of numpy.zeros"""
        return self.nplike_lib.zeros(self.queue, *args, **kwargs)

    def plan_FFT(self, data, axes, wait_on_call=True):
        """
        Generates an FFT plan object to be executed on the context.

        Args:
            data (pyopencl.array.Array): Array having type and shape for which
                the FFT needs to be planned.
            axes (sequence of ints): Axes along which the FFT needs to be
                performed.
        Returns:
            FFTPyopencl: FFT plan for the required array shape, type and axes.

        Example:

        .. code-block:: python

            plan = context.plan_FFT(data, axes=(0,1))

            data2 = 2*data

            # Forward tranform (in place)
            plan.transform(data2)

            # Inverse tranform (in place)
            plan.itransform(data2)
        """
        return FFTPyopencl(self, data, axes, wait_on_call)

    @property
    def kernels(self):
        return self._kernels


class BufferPyopencl(XBuffer):
    def _make_context(self):
        return ContextPyopencl()

    def _new_buffer(self, capacity):
        return cl.Buffer(
            self.context.context, cl.mem_flags.READ_WRITE, capacity
        )

    def copy_to(self, dest):
        # Does not pass through cpu if it can
        # dest: python object that uses buffer protocol or opencl buffer
        cl.enqueue_copy(self.context.queue, dest, self.buffer)

    def copy_from(self, source, src_offset, dest_offset, byte_count):
        # Does not pass through cpu if it can
        # source: python object that uses buffer protocol or opencl buffer
        cl.enqueue_copy(
            self.context.queue,
            self.buffer,
            source,
            src_offset,
            dest_offset,
            byte_count,
        )

    def write(self, offset, data):
        # From python object with buffer interface on cpu
        log.debug(f"write {self} {offset} {data}")
        cl.enqueue_copy(
            self.context.queue, self.buffer, data, device_offset=offset
        )

    def read(self, offset, size):
        # To bytearray on cpu
        data = bytearray(size)
        cl.enqueue_copy(
            self.context.queue, data, self.buffer, device_offset=offset
        )
        return data

    def update_from_native(
        self, offset: int, source: cl.Buffer, source_offset: int, nbytes: int
    ):
        """Copy data from native buffer into self.buffer starting from offset"""
        cl.enqueue_copy(
            self.context.queue,
            self.buffer,
            source,
            src_offset=source_offset,
            dest_offset=offset,
            byte_count=nbytes,
        )

    def copy_native(self, offset: int, nbytes: int):
        """return native data with content at from offset and nbytes"""
        buff = cl.Buffer(self.context.context, cl.mem_flags.READ_WRITE, nbytes)
        cl.enqueue_copy(
            queue=self.context.queue,
            dest=buff,
            src=self.buffer,
            src_offset=offset,
            byte_count=nbytes,
        )
        return buff

    def update_from_buffer(self, offset: int, source):
        """Copy data from python buffer such as bytearray, bytes, memoryview, numpy array.data"""
        cl.enqueue_copy(
            queue=self.context.queue,
            dest=self.buffer,
            src=source,  # nbytes taken from min(len(source),len(buffer))
            device_offset=offset,
        )

    def to_nplike(self, offset, dtype, shape):
        """view in nplike"""
        return cl.array.Array(
            self.context.queue,
            base_data=self.buffer,
            offset=offset,
            shape=shape,
        )

    def update_from_nplike(self, offset, dest_dtype, arr):
        if arr.dtype != dest_dtype:
            arr = arr.astype(dest_dtype)
        self.update_from_native(offset, arr.base_data, arr.offset, arr.nbytes)

    def to_bytearray(self, offset, nbytes):
        """copy in byte array: used in update_from_xbuffer"""
        data = bytearray(nbytes)
        cl.enqueue_copy(
            queue=self.context.queue,
            dest=data,  # nbytes taken from min(len(data),len(buffer))
            src=self.buffer,
            device_offset=offset,
        )
        return data

    def to_pointer_arg(self, offset, nbytes):
        """return data that can be used as argument in kernel

        Can fail if offset is not a multiple of self.alignment

        """
        return self.buffer[offset : offset + nbytes]


class KernelPyopencl(object):
    def __init__(
        self,
        function,
        description,
        context,
        wait_on_call=True,
    ):

        self.function = function
        self.description = description
        self.context = context
        self.wait_on_call = wait_on_call

    def to_function_arg(self, arg, value):
        if arg.pointer:
            if hasattr(arg.atype, "_dtype"):  # it is numerical scalar
                if hasattr(value, "dtype"):  # nparray
                    assert isinstance(value, cla.Array)
                    return value.base_data[value.offset :]
                elif hasattr(value, "_shape"):  # xobject array
                    raise NotImplementedError
            else:
                raise ValueError(
                    f"Invalid value {value} for argument {arg.name} "
                    f"of kernel {self.description.pyname}"
                )
        else:
            if hasattr(arg.atype, "_dtype"):  # it is numerical scalar
                return arg.atype(value)  # try to return a numpy scalar
            elif hasattr(arg.atype, "_size"):  # it is a compound xobject
                    raise NotImplementedError
            else:
                raise ValueError(
                    f"Invalid value {value} for argument {arg.name} of kernel {self.description.pyname}")

    @property
    def num_args(self):
        return len(self.description.args)

    def __call__(self, **kwargs):
        assert len(kwargs.keys()) == self.num_args
        arg_list = []
        for arg in self.description.args:
            vv = kwargs[arg.name]
            arg_list.append(self.to_function_arg(arg, vv))

        if isinstance(self.description.n_threads, str):
            n_threads = kwargs[self.description.n_threads]
        else:
            n_threads = self.description.n_threads

        event = self.function(
            self.context.queue, (n_threads,), None, *arg_list
        )

        if self.wait_on_call:
            event.wait()

        return event


class FFTPyopencl(object):
    def __init__(self, context, data, axes, wait_on_call=True):

        self.context = context
        self.axes = axes
        self.wait_on_call = wait_on_call

        assert len(data.shape) > max(axes)

        # Check internal dimensions are powers of two
        for ii in axes[:-1]:
            nn = data.shape[ii]
            frac_part, _ = np.modf(np.log(nn) / np.log(2))
            assert np.isclose(frac_part, 0), (
                "PyOpenCL FFT requires"
                " all dimensions apart from the last to be powers of two!"
            )

        import gpyfft

        self._fftobj = gpyfft.fft.FFT(
            context.context, context.queue, data, axes=axes
        )

    def transform(self, data):
        """The transform is done inplace"""

        (event,) = self._fftobj.enqueue_arrays(data)
        if self.wait_on_call:
            event.wait()
        return event

    def itransform(self, data):
        """The transform is done inplace"""

        (event,) = self._fftobj.enqueue_arrays(data, forward=False)
        if self.wait_on_call:
            event.wait()
        return event


if _enabled:
    available.append(ContextPyopencl)
