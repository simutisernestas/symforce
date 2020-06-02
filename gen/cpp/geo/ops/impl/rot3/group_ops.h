//  ----------------------------------------------------------------------------
// This file was autogenerated by symforce. Do NOT modify by hand.
// -----------------------------------------------------------------------------
#pragma once

#include <Eigen/Dense>

#include <geo/rot3.h>

namespace geo {
namespace rot3 {

/**
 * C++ GroupOps implementation for <class 'symforce.geo.rot3.Rot3'>.
 */
template <typename Scalar>
struct GroupOps {
  static Rot3<Scalar> Identity();
  static Rot3<Scalar> Inverse(const Rot3<Scalar>& a);
  static Rot3<Scalar> Compose(const Rot3<Scalar>& a, const Rot3<Scalar>& b);
  static Rot3<Scalar> Between(const Rot3<Scalar>& a, const Rot3<Scalar>& b);

};

}  // namespace rot3

// Wrapper to specialize the public concept

template <>
struct GroupOps<Rot3<double>> : public rot3::GroupOps<double> {};
template <>
struct GroupOps<Rot3<float>> : public rot3::GroupOps<float> {};

}  // namespace geo