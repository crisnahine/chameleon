class User < ApplicationRecord
  has_many :posts
  has_many :comments

  validates :email, presence: true, uniqueness: true
  validates :name, presence: true
end
