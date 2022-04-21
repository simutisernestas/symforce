# ----------------------------------------------------------------------------
# SymForce - Copyright 2022, Skydio, Inc.
# This source code is under the Apache 2.0 license found in the LICENSE file.
# ----------------------------------------------------------------------------

from __future__ import annotations

import copy
import enum
import functools
import os
import pathlib
from pathlib import Path
import tempfile
import textwrap

from symforce import sympy as sm
from symforce import geo
from symforce import jacobian_helpers
from symforce import ops
from symforce import logger
from symforce import python_util
from symforce import typing as T
from symforce.values import Values
from symforce.codegen import template_util
from symforce.codegen import codegen_util
from symforce.codegen import codegen_config
from symforce.codegen import types_package_codegen
from symforce.type_helpers import symbolic_inputs

CURRENT_DIR = os.path.dirname(__file__)


class LinearizationMode(enum.Enum):
    """
    Mode for with_linearization
    """

    # Compute jacobians for input arguments stacked into a single jacobian matrix
    STACKED_JACOBIAN = "stacked_jacobian"

    # Compute a full linearization for the output with respect to the given input arguments.  This
    # includes the jacobian, hessian (computed as J^T J with only the lower triangle filled out),
    # and rhs (J^T b).  In this mode, the original function must return a vector (a geo.Matrix with
    # one column).
    FULL_LINEARIZATION = "full_linearization"


class Codegen:
    """
    Class used for generating code from symbolic expressions or functions.

    Codegen objects can either be used to generate standalone functions, or
    as specifications in a larger code generation pipeline. Each codegen object
    defines an input/output relationship between a set of symbolic inputs and
    a set of symbolic output expressions written in terms of the inputs.
    """

    def __init__(
        self,
        inputs: Values,
        outputs: Values,
        config: codegen_config.CodegenConfig,
        name: T.Optional[str] = None,
        return_key: T.Optional[str] = None,
        sparse_matrices: T.List[str] = None,
        docstring: str = None,
    ) -> None:
        """
        Creates the Codegen specification.

        Args:
            inputs: Values object specifying names and symbolic inputs to the function
            outputs: Values object specifying names and output expressions (written in terms
                     of the symbolic inputs) of the function
            config: Programming language and configuration in which the function is to be generated
            name: Name of the function to be generated; must be set before the function is
                  generated, but need not be set here if it's going to be set by with_linearization
                  or with_jacobians.  Should be snake_case, will be converted to the
                  language-specific function name style at generation time
            return_key: If specified, the output with this key is returned rather than filled
                        in as a named output argument.
            sparse_matrices: Outputs with this key will be returned as sparse matrices
            docstring: The docstring to be used with the generated function
        """

        self.name = name

        # Inputs and outputs must be Values objects
        assert isinstance(inputs, Values)
        assert isinstance(outputs, Values)

        # All symbols in outputs must be present in inputs.  Convert to Matrix before calling
        # free_symbols because it's much faster to call once
        input_symbols_list = codegen_util.flat_symbols_from_values(inputs)
        input_symbols = set(input_symbols_list)
        assert sm.S(
            geo.Matrix(codegen_util.flat_symbols_from_values(outputs)).mat
        ).free_symbols.issubset(
            input_symbols
        ), f"A symbol in the output expression is missing from inputs. inputs={input_symbols}"

        # Names given by keys in inputs/outputs must be valid variable names
        # TODO(aaron): Also check recursively
        assert all(k.isidentifier() for k in inputs.keys())
        assert all(k.isidentifier() for k in outputs.keys())

        # Symbols in inputs must be unique
        assert len(input_symbols) == len(
            input_symbols_list
        ), "Symbols in inputs must be unique. Duplicate symbols = {}".format(
            [symbol for symbol in input_symbols_list if input_symbols_list.count(symbol) > 1]
        )

        # Outputs must not have same variable names/keys as inputs
        assert all(key not in list(outputs.keys()) for key in inputs.keys())

        self.inputs = inputs
        self.outputs = outputs

        self.config = config

        if return_key is not None:
            assert return_key in outputs
        self.return_key = return_key

        # Mapping between sparse matrix keys and constants needed for static CSC construction
        self.sparse_mat_data: T.Dict[str, codegen_util.CSCFormat] = {}
        if sparse_matrices is not None:
            assert all(key in outputs for key in sparse_matrices)
            assert all(isinstance(outputs[key], geo.Matrix) for key in sparse_matrices)
            for key in sparse_matrices:
                self.sparse_mat_data[key] = codegen_util.CSCFormat.from_matrix(outputs[key])

        self.docstring = (
            docstring or Codegen.default_docstring(inputs=inputs, outputs=outputs)
        ).rstrip()

        self.types_included: T.Optional[T.Set[str]] = None
        self.typenames_dict: T.Optional[T.Dict[str, str]] = None
        self.namespaces_dict: T.Optional[T.Dict[str, str]] = None
        self.unique_namespaces: T.Optional[T.Set[str]] = None
        self.namespace: T.Optional[str] = None

    @classmethod
    def function(
        cls,
        func: T.Callable,
        config: codegen_config.CodegenConfig,
        name: T.Optional[str] = None,
        input_types: T.Sequence[T.ElementOrType] = None,
        output_names: T.Sequence[str] = None,
        return_key: str = None,
        docstring: str = None,
    ) -> Codegen:
        """
        Creates a Codegen object from a symbolic python function.

        Args:
            func: Python function. Note, variable position and keyword arguments will be ignored.
                Additionally, keyword only arguments will be set to their default values and not
                included in the signature of the generated function.
            input_types: List of types of the inputs to the given function.  This is optional; if
                `func` has type annotations, `input_types` can be deduced from those.  Note that
                if the type annotation doesn't match what you want the arguments to be, you need
                to specify manually, for instance a function add(x: T.Any, y: T.Any) -> T.Any that
                you want to use to generate add(x: geo.Matrix33, y: geo.Matrix33) -> geo.Matrix33
            config: Programming language and configuration in which the function is to be generated
            name: Name of the function to be generated; if not provided, will be deduced from the
                function name.  Must be provided if `func` is a lambda
            output_names: Optional if only one object is returned by the function.
                If multiple objects are returned, they must be named.
            return_key: If multiple objects are returned, the generated function will return
                the object with this name (must be in output_names)
            docstring: The docstring to be used with the generated function.  Default is to use the
                       existing docstring
        """
        if name is None:
            inner_func = python_util.get_func_from_maybe_bound_function(func)
            assert inner_func.__name__ != "<lambda>", "Can't deduce name automatically for a lambda"
            name = inner_func.__name__

        inputs = symbolic_inputs(func, input_types)

        # Run the symbolic arguments through the function and get the symbolic output expression(s)
        res = func(*inputs.values())

        # at this point replace all dataclasses in the inputs with values
        inputs = inputs.dataclasses_to_values()

        if isinstance(res, tuple):
            # Function returns multiple objects
            output_terms = res
            assert output_names is not None, "Must give output_names for multiple outputs"
            # If a return key is given, it must be valid (i.e. in output_names)
            if return_key is not None:
                assert return_key in output_names, "Return key not found in named outputs"
        else:
            # Function returns single object
            output_terms = (res,)
            if output_names is None:
                output_names = ["res"]
                return_key = output_names[0]
        assert len(output_terms) == len(output_names)

        # Form the output expressions as a Values object
        outputs = Values()
        for output_name, output in zip(output_names, output_terms):
            if isinstance(output, (list, tuple)):
                output = geo.Matrix(output)
            outputs[output_name] = output

        # Pull docstring out of function if not provided
        if docstring is None:
            inner_func = python_util.get_func_from_maybe_bound_function(func)
            if inner_func.__doc__:
                docstring = inner_func.__doc__
            else:
                docstring = Codegen.default_docstring(
                    inputs=inputs, outputs=outputs, original_function=inner_func
                )

        return cls(
            name=name,
            inputs=inputs,
            outputs=outputs,
            config=config,
            return_key=return_key,
            docstring=textwrap.dedent(docstring),
        )

    @staticmethod
    def common_data() -> T.Dict[str, T.Any]:
        """
        Return common template data for code generation.
        """
        data: T.Dict[str, T.Any] = {}
        data["ops"] = ops
        data["Symbol"] = sm.Symbol
        data["Matrix"] = geo.Matrix
        data["DataBuffer"] = sm.DataBuffer
        data["Values"] = Values
        data["pathlib"] = pathlib
        data["path_to_codegen"] = CURRENT_DIR
        data["scalar_types"] = ("double", "float")
        data["camelcase_to_snakecase"] = python_util.camelcase_to_snakecase
        data["python_util"] = python_util
        data["lcm_type_t_include_dir"] = "<lcmtypes/sym/type_t.hpp>"

        def is_symbolic(T: T.Any) -> bool:
            return isinstance(T, (sm.Expr, sm.Symbol))

        data["is_symbolic"] = is_symbolic
        data["issubclass"] = issubclass
        data["is_sequence"] = lambda arg: isinstance(arg, (list, tuple))

        def should_set_zero(mat: geo.Matrix, zero_initialization_sparsity_threshold: float) -> bool:
            """
            Returns True if we should set a dense matrix to 0 and then only set nonzero elements,
            instead of setting all elements individually (including elements that are 0)

            Result is equivalent to `nnz / (M * N) < threshold`
            """
            nnz = 0
            for i in range(mat.shape[0]):
                for j in range(mat.shape[1]):
                    if mat[i, j] != 0:
                        nnz += 1
            return nnz / (mat.shape[0] * mat.shape[1]) < zero_initialization_sparsity_threshold

        data["should_set_zero"] = should_set_zero

        return data

    @functools.cached_property
    def print_code_results(self) -> codegen_util.PrintCodeResult:
        return codegen_util.print_code(
            inputs=self.inputs,
            outputs=self.outputs,
            sparse_mat_data=self.sparse_mat_data,
            config=self.config,
        )

    def generate_function(
        self,
        output_dir: T.Openable = None,
        lcm_bindings_output_dir: T.Openable = None,
        shared_types: T.Mapping[str, str] = None,
        namespace: str = "sym",
        generated_file_name: str = None,
        skip_directory_nesting: bool = False,
    ) -> T.Dict[str, T.Any]:
        """
        Generates a function that computes the given outputs from the given inputs.

        Usage for generating multiple functions with a shared type:
            codegen_obj_1.generate_function(namespace="my_namespace")
            shared_types = {"my_type": "my_namespace.my_type_t"}
            codegen_obj_2.generate_function(shared_types=shared_types, namespace="my_namespace")

        In the example above, both codegen_obj_1 and codegen_obj_2 use the type "my_type". During
        the first call to "generate_function" we generate the type "my_type", and it then becomes
        a shared type for the second call to "generate_function". This signals that "my_type" does
        not need to be generated during the second call to "generate_function" as it already exists.

        Args:
            output_dir: Directory in which to output the generated function. Any generated types will
                be located in a subdirectory with name equal to the namespace argument.
            lcm_bindings_output_dir: Directory in which to output language-specific LCM bindings
            shared_types: Mapping between types defined as part of this codegen object (e.g. keys in
                self.inputs that map to Values objects) and previously generated external types.
            namespace: Namespace for the generated function and any generated types.
            generated_file_name: Stem for the filename into which the function is generated, with
                                 no file extension
            skip_directory_nesting: Generate the output file directly into output_dir instead of
                                    adding the usual directory structure inside output_dir
        """
        assert (
            self.name is not None
        ), "Name should be set either at construction or by with_jacobians"

        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix=f"sf_codegen_{self.name}_", dir="/tmp"))
            logger.debug(f"Creating temp directory: {output_dir}")
        elif isinstance(output_dir, str):
            output_dir = Path(output_dir)
        assert isinstance(output_dir, Path)

        if lcm_bindings_output_dir is None:
            lcm_bindings_output_dir = output_dir
        elif isinstance(lcm_bindings_output_dir, str):
            lcm_bindings_output_dir = Path(lcm_bindings_output_dir)
        assert isinstance(lcm_bindings_output_dir, Path)

        if generated_file_name is None:
            generated_file_name = self.name

        # List of (template_path, output_path, data)
        templates = template_util.TemplateList()

        # Output types
        # Find each Values object in the inputs and outputs
        types_to_generate = []
        # Also keep track of non-Values types used so we can have the proper includes - things like
        # geo types and cameras
        self.types_included = set()
        for d in (self.inputs, self.outputs):
            for key, value in d.items():
                # If "value" is a list, extract an instance of a base element.
                base_value = codegen_util.get_base_instance(value)

                if isinstance(base_value, Values):
                    types_to_generate.append((key, base_value))
                else:
                    self.types_included.add(type(base_value).__name__)

        # Generate types from the Values objects in our inputs and outputs
        values_indices = {name: gen_type.index() for name, gen_type in types_to_generate}
        types_codegen_data = types_package_codegen.generate_types(
            package_name=namespace,
            file_name=generated_file_name,
            values_indices=values_indices,
            use_eigen_types=self.config.use_eigen_types,
            shared_types=shared_types,
            output_dir=os.fspath(output_dir),
            lcm_bindings_output_dir=os.fspath(lcm_bindings_output_dir),
            templates=templates,
        )

        # Maps typenames to generated types
        self.typenames_dict = types_codegen_data["typenames_dict"]
        # Maps typenames to namespaces
        self.namespaces_dict = types_codegen_data["namespaces_dict"]
        assert self.namespaces_dict is not None
        self.unique_namespaces = set(self.namespaces_dict.values())

        # Namespace of this function + generated types
        self.namespace = namespace

        output_data = {
            "output_dir": output_dir,
            "lcm_type_dir": types_codegen_data["lcm_type_dir"],
        }

        template_data = dict(self.common_data(), spec=self)

        # Generate the function
        if isinstance(self.config, codegen_config.PythonConfig):
            if skip_directory_nesting:
                python_function_dir = output_dir
            else:
                python_function_dir = output_dir / "python" / "symforce" / namespace

            logger.info(f'Creating python function "{self.name}" at "{python_function_dir}"')

            templates.add(
                Path(template_util.PYTHON_TEMPLATE_DIR) / "function" / "FUNCTION.py.jinja",
                python_function_dir / f"{generated_file_name}.py",
                template_data,
            )
            templates.add(
                Path(template_util.PYTHON_TEMPLATE_DIR) / "function" / "__init__.py.jinja",
                python_function_dir / "__init__.py",
                template_data,
            )

            output_data["python_function_dir"] = python_function_dir
        elif isinstance(self.config, codegen_config.CppConfig):
            if skip_directory_nesting:
                cpp_function_dir = output_dir
            else:
                cpp_function_dir = output_dir / "cpp" / "symforce" / namespace

            logger.info(
                f'Creating C++ function "{python_util.snakecase_to_camelcase(self.name)}" at "{cpp_function_dir}"'
            )

            templates.add(
                Path(template_util.CPP_TEMPLATE_DIR) / "function" / "FUNCTION.h.jinja",
                cpp_function_dir / f"{generated_file_name}.h",
                template_data,
            )

            output_data["cpp_function_dir"] = cpp_function_dir
        else:
            raise NotImplementedError(f'Unknown config type: "{self.config}"')

        templates.render(autoformat=self.config.autoformat)
        lcm_data = codegen_util.generate_lcm_types(
            lcm_type_dir=types_codegen_data["lcm_type_dir"],
            lcm_files=types_codegen_data["lcm_files"],
            lcm_output_dir=types_codegen_data["lcm_bindings_output_dir"],
        )
        output_data.update(lcm_data)

        output_data["generated_files"] = [v[1] for v in templates.items]

        return output_data

    @staticmethod
    def default_docstring(
        inputs: Values, outputs: Values, original_function: T.Callable = None
    ) -> str:
        """
        Create a default docstring if no other is available from the function or caller.
        """
        # If the function is an instance method, remove the type associated with the class
        input_names = [name for name, arg in inputs.items() if name != "self"]

        def nice_typename(arg: T.Any) -> str:
            if python_util.scalar_like(arg):
                return "Scalar"
            else:
                return python_util.get_type(arg).__name__

        input_types = [nice_typename(arg) for name, arg in inputs.items() if name != "self"]
        output_types = [nice_typename(arg) for arg in outputs.values()]

        if original_function is not None:
            docstring = f"""
            This function was autogenerated from a symbolic function. Do not modify by hand.

            Symbolic function: {original_function.__name__}

            Args:
            """
        else:
            docstring = """
            This function was autogenerated. Do not modify by hand.

            Args:
            """

        arg_descriptions = "".join(
            [f"    {name}: {input_type}\n" for name, input_type in zip(input_names, input_types)]
        )

        output_descriptions = "".join(
            [
                f"    {name}: {output_type}\n"
                for name, output_type in zip(outputs.keys(), output_types)
            ]
        )

        return textwrap.dedent(docstring) + arg_descriptions + "\nOutputs:\n" + output_descriptions

    @staticmethod
    def wrap_docstring_arg_description(
        preamble: str, description: str, config: codegen_config.CodegenConfig
    ) -> T.List[str]:
        return textwrap.wrap(
            description,
            width=config.line_length - len(config.doc_comment_line_prefix),
            initial_indent=preamble,
            subsequent_indent=" " * len(preamble),
        )

    def _pick_name_for_function_with_derivatives(
        self,
        which_args: T.Sequence[str],
        include_results: bool,
        linearization_mode: T.Optional[LinearizationMode],
    ) -> str:
        assert (
            self.name is not None
        ), "Codegen name must have been provided already to automatically generate a name with derivatives"

        name = self.name
        if linearization_mode == LinearizationMode.FULL_LINEARIZATION:
            if name.endswith("_residual"):
                name = name[: -len("_residual")]

            if not name.endswith("_factor"):
                name += "_factor"
        else:
            if include_results:
                name += "_with"

            jacobians = python_util.plural("_jacobian", len(which_args))
            if len(which_args) == len(self.inputs):
                name += jacobians
            else:
                inputs_keys = list(self.inputs.keys())
                name += jacobians + "".join(str(inputs_keys.index(s)) for s in which_args)

        return name

    def with_linearization(
        self,
        which_args: T.Sequence[str] = None,
        include_result: bool = True,
        name: str = None,
        linearization_mode: LinearizationMode = LinearizationMode.FULL_LINEARIZATION,
        sparse_linearization: bool = False,
        custom_jacobian: geo.Matrix = None,
    ) -> Codegen:
        """
        Given a codegen object that takes some number of inputs and computes a single result,
        create a new codegen object that additionally computes the jacobian (or the full
        Gauss-Newton linearization) with respect to the given input arguments.

        The jacobians are in the tangent spaces of the inputs and outputs, see jacobian_helpers.py
        for more information.

        The previous codegen object (the `self` argument to this function) is unmodified by this
        function and still valid after this function returns.

        Args:
            self: Existing codegen object that returns a single value
            which_args: Names of args for which to compute jacobians. If not given, uses all.
            include_result: For the STACKED_JACOBIAN mode, whether we should still include the
                            result or only return the jacobian.  For the FULL_LINEARIZATION mode, we
                            always include the result (which is the residual).
            name: Generated function name. If not given, picks a reasonable name based on the one
                  given at construction.
            linearization_mode: Whether to generate a single jacobian matrix (STACKED_JACOBIANS), or
                                generate a full linearization with a hessian and rhs
                                (FULL_LINEARIZATION).
            sparse_linearization: Whether to output matrices (jacobian and/or hessian) as sparse
                                  matrices, as opposed to dense
            custom_jacobian: This is generally unnecessary, unless you want to override the jacobian
                             computed by SymForce, e.g. to stop derivatives with respect to certain
                             variables or directions, or because the jacobian can be analytically
                             simplified in a way that SymForce won't do automatically. If not
                             provided, the jacobian will be computed automatically.  If provided,
                             should have shape (result_dim, input_tangent_dim), where
                             input_tangent_dim is the sum of the tangent dimensions of arguments
                             corresponding to which_args
        """
        if which_args is None:
            which_args = list(self.inputs.keys())

        assert which_args, "Cannot compute a linearization with respect to 0 arguments"

        # Ensure the previous codegen has one output
        assert len(list(self.outputs.keys())) == 1
        result_name, result = list(self.outputs.items())[0]

        # Get docstring
        docstring_lines = self.docstring.rstrip().split("\n")

        # Make the new outputs
        outputs = Values()
        if include_result:
            outputs[result_name] = result
        else:
            # Remove return val line from docstring
            docstring_lines = docstring_lines[:-1]

        input_args = [self.inputs[arg] for arg in which_args]
        if custom_jacobian is not None:
            jacobian = custom_jacobian
        else:
            jacobian = geo.Matrix.block_matrix(
                [jacobian_helpers.tangent_jacobians(result, input_args)]
            )

        docstring_args = [
            f"{arg_name} ({ops.LieGroupOps.tangent_dim(arg)})"
            for arg_name, arg in zip(which_args, input_args)
        ]

        formatted_arg_list = "{} {}".format(
            python_util.plural("arg", len(docstring_args)), ", ".join(docstring_args)
        )

        docstring_lines.extend(
            self.wrap_docstring_arg_description(
                "    jacobian: ",
                f"({jacobian.shape[0]}x{jacobian.shape[1]}) jacobian of {result_name} wrt {formatted_arg_list}",
                self.config,
            )
        )

        outputs["jacobian"] = jacobian

        if linearization_mode == LinearizationMode.FULL_LINEARIZATION:
            assert (
                isinstance(result, geo.Matrix) and result.cols == 1
            ), f"The output must be a vector (the residual), got {result} instead"

            hessian = jacobian.compute_AtA(lower_only=True)
            outputs["hessian"] = hessian
            docstring_lines.extend(
                self.wrap_docstring_arg_description(
                    "    hessian: ",
                    f"({hessian.shape[0]}x{hessian.shape[1]}) Gauss-Newton hessian for {formatted_arg_list}",
                    self.config,
                )
            )

            rhs = jacobian.T * result
            outputs["rhs"] = rhs
            docstring_lines.extend(
                self.wrap_docstring_arg_description(
                    "    rhs: ",
                    f"({rhs.shape[0]}x{rhs.shape[1]}) Gauss-Newton rhs for {formatted_arg_list}",
                    self.config,
                )
            )

        # If just computing a single jacobian, return it instead of output arg
        return_key = list(outputs.keys())[0] if len(list(outputs.keys())) == 1 else None

        # Cutely pick a function name if not given
        if not name:
            name = self._pick_name_for_function_with_derivatives(
                which_args, include_result, linearization_mode
            )

        sparse_matrices = (
            [key for key in ("jacobian", "hessian") if key in outputs]
            if sparse_linearization
            else None
        )
        return Codegen(
            name=name,
            inputs=self.inputs,
            outputs=outputs,
            config=self.config,
            return_key=return_key,
            sparse_matrices=sparse_matrices,
            docstring="\n".join(docstring_lines),
        )

    def with_jacobians(
        self,
        which_args: T.Sequence[str] = None,
        which_results: T.Sequence[int] = (0,),
        include_results: bool = True,
        name: str = None,
        sparse_jacobians: bool = False,
    ) -> Codegen:
        """
        Given a codegen object that takes some number of inputs and computes some number of results,
        create a new codegen object that additionally computes jacobians of the given results with
        respect to the given input arguments. By default, computes the jacobians of the first result
        with respect to all arguments.  Flexible to produce the values and all jacobians, just the
        jacobians, or any combination of one or more jacobians.

        The jacobians are in the tangent spaces of the inputs and outputs, see jacobian_helpers.py
        for more information.

        The previous codegen object (the `self` argument to this function) is unmodified by this
        function and still valid after this function returns.

        Args:
            self: Existing codegen object that return a single value
            which_args: Names of args for which to compute jacobians. If not given, uses all.
            which_results: Indices of results for which to compute jacobians.  If not given, uses
                           the first result.
            include_results: Whether we should still return the values in addition to the
                             jacobian(s), for the results in which_results.  Values not in
                             which_results are always still returned.
            name: Generated function name. If not given, picks a reasonable name based on the one
                  given at construction.
            sparse_jacobians: Whether to output jacobians as sparse matrices, as opposed to dense
        """
        if which_args is None:
            which_args = list(self.inputs.keys())

        assert which_args, "Cannot compute a linearization with respect to 0 arguments"

        assert list(sorted(which_results)) == list(which_results), "which_results must be sorted"

        # Get docstring
        docstring_lines = self.docstring.rstrip().split("\n")

        # Make the new outputs
        if include_results:
            outputs = copy.deepcopy(self.outputs)
        else:
            outputs = Values()

            # Copy in results we're not differentiating
            self_outputs_keys = list(self.outputs.keys())
            for i in range(len(self.outputs)):
                if i not in which_results:
                    outputs[self_outputs_keys[i]] = self.outputs[self_outputs_keys[i]]

            # Remove return val lines from docstring
            # TODO(aaron): Make this work when some return values have multi-line descriptions
            for i in which_results:
                index_from_back = -len(self.outputs) + i
                del docstring_lines[index_from_back]

        # Add all the jacobians
        input_args = [self.inputs[arg] for arg in which_args]

        all_outputs = list(self.outputs.items())
        all_jacobian_names = []
        for i in which_results:
            result_name, result = all_outputs[i]

            arg_jacobians = jacobian_helpers.tangent_jacobians(result, input_args)

            for arg_name, arg, arg_jacobian in zip(which_args, input_args, arg_jacobians):
                jacobian_name = f"{result_name}_D_{arg_name}"
                outputs[jacobian_name] = arg_jacobian
                all_jacobian_names.append(jacobian_name)

                result_dim = ops.LieGroupOps.tangent_dim(result)
                arg_dim = ops.LieGroupOps.tangent_dim(arg)
                docstring_lines.append(
                    f"    {jacobian_name}: ({result_dim}x{arg_dim}) jacobian of "
                    + f"{result_name} ({result_dim}) wrt arg {arg_name} ({arg_dim})"
                )

        if len(outputs) == 1:
            # If just computing a single jacobian and nothing else, return it instead of output arg
            return_key: T.Optional[str] = list(outputs.keys())[0]
        elif self.return_key is not None and self.return_key in outputs:
            # If still computing the original return value, return that
            return_key = self.return_key
        else:
            return_key = None

        # Cutely pick a function name if not given
        if not name:
            name = self._pick_name_for_function_with_derivatives(
                which_args, include_results, linearization_mode=None
            )

        sparse_matrices = all_jacobian_names if sparse_jacobians else None
        return Codegen(
            name=name,
            inputs=self.inputs,
            outputs=outputs,
            config=self.config,
            return_key=return_key,
            sparse_matrices=sparse_matrices,
            docstring="\n".join(docstring_lines),
        )
