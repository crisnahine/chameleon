class User < ApplicationRecord
  has_many :orders

  validates :email, presence: true, uniqueness: true
  validates :name, presence: true
end
