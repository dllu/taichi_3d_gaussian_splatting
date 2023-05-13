import torch
from dataclasses import dataclass
from GaussianPointCloudRasterisation import GaussianPointCloudRasterisation


class GaussianPointAdaptiveController:
    """
    For simplicity, I set the size of point cloud to be fixed during training. an extra mask is used to indicate whether a point is invalid or not.
    When initialising, the input point cloud is concatenated with extra points filled with zero. The mask is concatenated with extra True.
    When densifying and splitting points, new points are assigned to locations of invalid points.
    When removing points, we just set the mask to True.
    """
    @dataclass
    class GaussianPointAdaptiveControllerConfig:
        num_iterations_warm_up: int = 500
        num_iterations_densify: int = 100
        # from paper: densify every 100 iterations and remove any Gaussians that are essentially transparent, i.e., with 𝛼 less than a threshold 𝜖𝛼.
        transparent_alpha_threshold: float = 0.
        # from paper: densify Gaussians with an average magnitude of view-space position gradients above a threshold 𝜏pos, which we set to 0.0002 in our tests.
        # I have no idea why their threshold is so low, may be their view space is normalized to [0, 1]?
        # TODO: find out a proper threshold
        densification_view_space_position_gradients_threshold: float = 0.002
        # from paper:  large Gaussians in regions with high variance need to be split into smaller Gaussians. We replace such Gaussians by two new ones, and divide their scale by a factor of 𝜙 = 1.6
        gaussian_split_factor_phi: float = 1.6
        # in paper section 5.2, they describe a method to moderate the increase in the number of Gaussians is to set the 𝛼 value close to zero every
        # 3000 iterations. I have no idea how it is implemented. I just assume that it is a reset of 𝛼 to fixed value.
        num_iterations_reset_alpha: int = 3000
        reset_alpha_value: float = 0.1
        # the paper doesn't mention this value, but we need a value and method to determine whether a point is under-reconstructed or over-reconstructed
        # for now, the method is to threshold norm of exp(s)
        # TODO: find out a proper threshold
        under_reconstructed_s_threshold: float = 0.1

    @dataclass
    class GaussianPointAdaptiveControllerMaintainedParameters:
        pointcloud: torch.Tensor  # shape: [num_points, 3]
        # shape: [num_points, num_features], num_features is 56
        pointcloud_features: torch.Tensor
        # shape: [num_points], dtype: int8 because taichi doesn't support bool type
        point_invalid_mask: torch.Tensor

    def __init__(self,
                 config: GaussianPointAdaptiveControllerConfig,
                 maintained_parameters: GaussianPointAdaptiveControllerMaintainedParameters):
        self.iteration_counter = 0
        self.config = config
        self.maintained_parameters = maintained_parameters

    def update(self, input_data: GaussianPointCloudRasterisation.BackwardValidPointHookInput):
        self.iteration_counter += 1
        if self.iteration_counter < self.config.num_iterations_warm_up:
            return
        if self.iteration_counter % self.config.num_iterations_densify == 0:
            self.densify(input_data)
        if self.iteration_counter % self.config.num_iterations_reset_alpha == 0:
            self.reset_alpha(input_data)

    def densify(self, input_data: GaussianPointCloudRasterisation.BackwardValidPointHookInput):
        pointcloud = self.maintained_parameters.pointcloud
        pointcloud_features = self.maintained_parameters.pointcloud_features
        point_alpha = pointcloud_features[:, 7]  # alpha before sigmoid
        point_to_remove_mask = point_alpha < self.config.transparent_alpha_threshold
        total_valid_points_before_densify = self.maintained_parameters.point_invalid_mask.shape[0] - \
            self.maintained_parameters.point_invalid_mask.sum()
        # remove any Gaussians that are essentially transparent
        self.maintained_parameters.point_invalid_mask[point_to_remove_mask] = 1
        print(f"remove {point_to_remove_mask.sum()} points")

        num_of_invalid_points = self.maintained_parameters.point_invalid_mask.sum()
        # split Gaussians with an average magnitude of view-space position gradients above a threshold
        # shape: [num_points_in_camera, 2]
        grad_viewspace = input_data.grad_viewspace
        point_in_camera_id: torch.Tensor = input_data.point_in_camera_id
        # shape: [num_points_in_camera, num_features]
        point_features_in_camera = pointcloud_features[point_in_camera_id]
        # all these three masks are on num_points_in_camera, not num_points
        to_densify_mask = grad_viewspace.norm(
            dim=1) > self.config.densification_view_space_position_gradients_threshold
        # shape: [num_points_in_camera, 3]
        point_s_in_camera = point_features_in_camera[:, 4:7]
        under_reconstructed_mask = to_densify_mask & (point_s_in_camera.exp().norm(
            dim=1) < self.config.under_reconstructed_s_threshold)
        over_reconstructed_mask = to_densify_mask & (~under_reconstructed_mask)

        under_reconstructed_point_id = point_in_camera_id[under_reconstructed_mask]
        over_reconstructed_point_id = point_in_camera_id[over_reconstructed_mask]

        under_reconstructed_points = pointcloud[under_reconstructed_point_id]
        over_reconstructed_points = pointcloud[over_reconstructed_point_id]
        under_reconstructed_point_features = point_features_in_camera[under_reconstructed_mask]
        over_reconstructed_point_features = point_features_in_camera[over_reconstructed_mask]
        over_reconstructed_point_features[:,
                                          4:7] /= self.config.gaussian_split_factor_phi

        densify_points = torch.cat(
            [under_reconstructed_points, over_reconstructed_points], dim=0)
        densify_point_features = torch.cat(
            [under_reconstructed_point_features, over_reconstructed_point_features], dim=0)
        num_of_densify_points = densify_points.shape[0]
        if num_of_densify_points > num_of_invalid_points:
            densify_points = densify_points[:num_of_invalid_points]
            densify_point_features = densify_point_features[:num_of_invalid_points]
        num_of_densify_points = densify_points.shape[0]
        # find the first num_of_densify_points invalid points
        invalid_point_id = torch.where(self.maintained_parameters.point_invalid_mask == 1)[
            0][:num_of_densify_points]
        self.maintained_parameters.pointcloud[invalid_point_id] = densify_points
        self.maintained_parameters.pointcloud_features[invalid_point_id] = densify_point_features
        self.maintained_parameters.point_invalid_mask[invalid_point_id] = 0
        total_valid_points = self.maintained_parameters.point_invalid_mask.shape[0] - \
            self.maintained_parameters.point_invalid_mask.sum()
        print(
            f"total valid points: {total_valid_points_before_densify} -> {total_valid_points}, under reconstructed points: {under_reconstructed_points.shape[0]}, over reconstructed points: {over_reconstructed_points.shape[0]}")

    def reset_alpha(self, input_data: GaussianPointCloudRasterisation.BackwardValidPointHookInput):
        pointcloud_features = self.maintained_parameters.pointcloud_features
        pointcloud_features[:, 7] = self.config.reset_alpha_value