# ----------------------------------------------------------------------------
# SymForce - Copyright 2022, Skydio, Inc.
# This source code is under the Apache 2.0 license found in the LICENSE file.
# ----------------------------------------------------------------------------

import re
import textwrap

import symforce.symbolic as sf
from symforce import codegen
from symforce import logger
from symforce import typing as T
from symforce.codegen import geo_factors_codegen
from symforce.codegen.slam_factors_codegen import inverse_range_landmark_prior_residual
from symforce.codegen.slam_factors_codegen import inverse_range_landmark_reprojection_error_residual
from symforce.values import Values

from .build_values import build_values


class FixedBundleAdjustmentProblem:
    """
    The setup is that we have N camera views for which we have poses that we want to refine.
    Camera 0 is taken as the source camera - we don't optimize its pose and treat it as the
    source for all matches. We have feature correspondences from camera 0 into each other camera.
    We put a prior on the relative poses between successive views, and the inverse range of each
    landmark.

    This is called from symforce/test/symforce_examples_bundle_adjustment_fixed_size_codegen_test.py
    to actually generate the problem
    """

    def __init__(self, num_views: int, num_landmarks: int) -> None:
        """
        Args:
            num_views: Number of poses/images given
            num_landmarks: Number of landmarks in base camera image
        """

        self.num_views = num_views
        self.num_landmarks = num_landmarks

        # Define symbols and store them in a Values object
        self.values = build_values(num_views=num_views, num_landmarks=num_landmarks)

        # Build residual
        self.residual = self._build_residual()

    def generate(self, output_dir: str) -> None:
        """
        Generates functions from symbolic expressions
        """

        logger.info("Generating linearization function for fixed-size problem")

        linearization_func = self._build_codegen_object()

        namespace = "bundle_adjustment_fixed_size"
        linearization_func.generate_function(output_dir=output_dir, namespace=namespace)

    def _build_codegen_object(self) -> codegen.Codegen:
        """
        Create Codegen object for the linearization function
        """
        logger.info("Building linearization function")

        flat_keys = {key: re.sub(r"[\.\[\]]+", "_", key) for key in self.values.keys_recursive()}

        inputs = Values(**{flat_keys[key]: value for key, value in self.values.items_recursive()})
        outputs = Values(residual=sf.M(self.residual.to_storage()))

        linearization_func = codegen.Codegen(
            inputs=inputs,
            outputs=outputs,
            config=codegen.CppConfig(),
            docstring=textwrap.dedent(
                """
                This function was autogenerated. Do not modify by hand.

                Computes the linearization of the residual around the given state,
                and returns the relevant information about the resulting linear system.

                Input args: The state to linearize around

                Output args:
                    residual (Eigen::Matrix*): The residual vector
                """
            ),
        ).with_linearization(
            name="linearization",
            which_args=[flat_keys[key] for key in self._optimized_keys()],
            sparse_linearization=True,
        )

        return linearization_func

    def _optimized_keys(self) -> T.List[str]:
        """
        Return a list of keys to be optimized:

         * Pose for each camera view except for 0 which is assumed fixed.
         * Landmark inverse range for each feature match.
        """
        # We fix the pose of view 0 so that the whole problem is constrained; alternatively, we
        # could add a prior on the pose of view 0 and leave it optimized
        return [f"views[{cam_index}].pose" for cam_index in range(1, self.num_views)] + [
            f"landmarks[{i}]" for i in range(self.num_landmarks)
        ]

    def _build_residual(self) -> Values:
        """
        Build the symbolic residual for which we will minimize the sum of squares.
        """
        residual = Values()
        residual["pose_prior"] = []
        residual["reprojection"] = []
        residual["inv_range_prior"] = []

        # Relative pose priors from all views to all views
        for src_cam_index in range(self.num_views):
            pose_priors = []
            for target_cam_index in range(self.num_views):
                # Do not put a prior on myself
                if src_cam_index == target_cam_index:
                    continue
                pose_priors.append(
                    geo_factors_codegen.between_factor(
                        self.values["views"][src_cam_index]["pose"],
                        self.values["views"][target_cam_index]["pose"],
                        self.values["priors"][src_cam_index][target_cam_index]["target_T_src"],
                        self.values["priors"][src_cam_index][target_cam_index]["sqrt_info"],
                        self.values["epsilon"],
                    )
                )
            residual["pose_prior"].append(pose_priors)

        for v_i in range(1, self.num_views):
            reprojections = []
            inv_range_priors = []
            for l_i in range(self.num_landmarks):
                match = self.values["matches"][v_i - 1][l_i]

                # Feature match reprojection error (huberized)
                reprojections.append(
                    inverse_range_landmark_reprojection_error_residual(
                        self.values["views"][0]["pose"],
                        self.values["views"][0]["calibration"],
                        self.values["views"][v_i]["pose"],
                        self.values["views"][v_i]["calibration"],
                        self.values["landmarks"][l_i],
                        match["source_coords"],
                        match["target_coords"],
                        match["weight"],
                        self.values["costs"]["reprojection_error_gnc_mu"],
                        self.values["costs"]["reprojection_error_gnc_scale"],
                        self.values["epsilon"],
                        sf.LinearCameraCal,
                    )
                )

                # Landmark inverse range prior
                inv_range_priors.append(
                    inverse_range_landmark_prior_residual(
                        self.values["landmarks"][l_i],
                        match["inverse_range_prior"],
                        match["weight"],
                        match["inverse_range_prior_sigma"],
                        self.values["epsilon"],
                    )[0]
                )
            residual["reprojection"].append(reprojections)
            residual["inv_range_prior"].append(inv_range_priors)

        return residual
