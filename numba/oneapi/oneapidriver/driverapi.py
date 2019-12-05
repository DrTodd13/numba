from cffi import FFI
import os
import sys


ffibuilder = FFI()

oneapiGlueHome = os.path.dirname(os.path.realpath(__file__))

BAD_ENV_PATH_ERRMSG = """
NUMBA_ONEAPI_GLUE_HOME is set to '{0}' which is not a valid path to a
dynamic link library for your system.
"""


def _raise_bad_env_path(path, extra=None):
    error_message = BAD_ENV_PATH_ERRMSG.format(path)
    if extra is not None:
        error_message += extra
    raise ValueError(error_message)

#oneapiGlueHome = os.environ.get('NUMBA_ONEAPI_GLUE_HOME', None)

# if oneapiGlueHome is None:
#    raise ValueError("FATAL: Set the NUMBA_ONEAPI_GLUE_HOME for "
#                     "numba_oneapi_glue.h and libnumbaoneapiglue.so")


if oneapiGlueHome is not None:
    try:
        oneapi_glue_home = os.path.abspath(oneapiGlueHome)
    except ValueError:
        _raise_bad_env_path(oneapiGlueHome)

    if not os.path.isfile(oneapiGlueHome + "/libnumbaoneapiglue.a"):
        _raise_bad_env_path(oneapiGlueHome + "/libnumbaoneapiglue.a")

# cdef() expects a single string declaring the C types, functions and
# globals needed to use the shared object. It must be in valid C syntax.
ffibuilder.cdef("""
    enum NUMBA_ONEAPI_GLUE_ERROR_CODES
    {
        NUMBA_ONEAPI_SUCCESS = 0,
        NUMBA_ONEAPI_FAILURE = -1
    };


    typedef enum NUMBA_ONEAPI_GLUE_MEM_FLAGS
    {
        NUMBA_ONEAPI_READ_WRITE = 0x0,
        NUMBA_ONEAPI_WRITE_ONLY,
        NUMBA_ONEAPI_READ_ONLY,
    } mem_flags_t;


    struct numba_oneapi_env_t
    {
        void *context;
        void *device;
        void *queue;
        unsigned int max_work_item_dims;
        int (*dump_fn) (void *);
    };

    typedef struct numba_oneapi_env_t* env_t;

    struct numba_oneapi_buffer_t
    {
        void *buffer;
    };

    typedef struct numba_oneapi_buffer_t* buffer_t;


    struct numba_oneapi_kernel_t
    {
        void *kernel;
    };

    typedef struct numba_oneapi_kernel_t* kernel_t;


    struct numba_oneapi_program_t
    {
        void *program;
    };

    typedef struct numba_oneapi_program_t* program_t;


    struct numba_oneapi_runtime_t
    {
        unsigned num_platforms;
        void *platform_ids;
        bool has_cpu;
        bool has_gpu;
        env_t first_cpu_env;
        env_t first_gpu_env;
        int (*dump_fn) (void *);
    };

    typedef struct numba_oneapi_runtime_t* runtime_t;

    int create_numba_oneapi_runtime (runtime_t *rt);
    
    int destroy_numba_oneapi_runtime (runtime_t *rt);
    
    int create_numba_oneapi_rw_mem_buffer (env_t env_t_ptr,
                                           size_t buffsize,
                                           buffer_t *buff);
    int destroy_numba_oneapi_rw_mem_buffer (buffer_t *buff);
    
    int write_numba_oneapi_mem_buffer_to_device (env_t env_t_ptr,
                                                 buffer_t buff,
                                                 bool blocking_copy,
                                                 size_t offset,
                                                 size_t buffersize,
                                                 const void* d_ptr);
                                                 
    int read_numba_oneapi_mem_buffer_from_device (env_t env_t_ptr,
                                                  buffer_t buff,
                                                  bool blocking_copy,
                                                  size_t offset,
                                                  size_t buffersize,
                                                  void* data_ptr);

    int create_numba_oneapi_program_from_spirv (env_t env_t_ptr,
                                                const void *il,
                                                size_t length,
                                                program_t *program_t_ptr);

    int create_numba_oneapi_program_from_source (env_t env_t_ptr,
                                                 unsigned int count,
                                                 const char **strings,
                                                 const size_t *lengths,
                                                 program_t *program_t_ptr);

    int destroy_numba_oneapi_program (program_t *program_t_ptr);

    int build_numba_oneapi_program (env_t env_t_ptr, program_t program_t_ptr);

    int create_numba_oneapi_kernel (env_t env_t_ptr,
                                    program_t program_ptr,
                                    const char *kernel_name,
                                    kernel_t *kernel_ptr);


    int destroy_numba_oneapi_kernel (kernel_t *kernel_ptr);

    int retain_numba_oneapi_context (env_t env_t_ptr);

    int release_numba_oneapi_context (env_t env_t_ptr);
            """)

ffi_lib_name = "numba.oneapi.oneapidriver._numba_oneapi_pybindings"

ffibuilder.set_source(
    ffi_lib_name,
    """
         #include "numba_oneapi_glue.h"   // the C header of the library
    """,
    libraries=["numbaoneapiglue", "OpenCL"],
    include_dirs=[oneapiGlueHome],
    library_dirs=[oneapiGlueHome]
)   # library name, for the linker


if __name__ == "__main__":
    # ffibuilder.emit_c_code("pybindings.c")
    ffibuilder.compile(verbose=True)